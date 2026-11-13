"""Cross-tenant isolation of the gateway-owned ``/api/jobs`` routes.

The gateway owns these records: they are Kubernetes Jobs in one shared
namespace, created and served by ``routers/jobs.py`` + ``services/job_manager.py``
(no backend is involved). Before this suite, list/get/delete took ``tenant_id``
and ignored it, so any tenant could read another's pod logs or delete its job.

Nothing here mocks the job manager. Requests go through the real router into
the real ``KubernetesJobManager``, which talks to an in-memory stand-in for the
two Kubernetes APIs that honours equality label selectors the way the apiserver
does. So a regression in EITHER layer (router forgets to pass the tenant, or the
manager forgets to filter) turns these red.

The tenant comes from the verified identity: ``require_tenant`` is overridden
per test to play the role of the licence-verified resolver. Every request also
carries a forged ``X-Tenant-Id`` naming the OTHER tenant; the routes must never
read it. (In production the gateway runs ``TRUST_TENANT_HEADER=false``, so
``require_tenant`` itself resolves from the licence, not the header.)
"""

from __future__ import annotations

import itertools
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

os.environ.setdefault("ENVIRONMENT", "test")

from kubernetes.client.rest import ApiException

TENANT_A = "tenant-a-0001"
TENANT_B = "tenant-b-0002"


# ---------------------------------------------------------------------------
# In-memory Kubernetes stand-in
# ---------------------------------------------------------------------------


def _matches(labels: dict, selector: str | None) -> bool:
    """Equality-based label selector, the only form the manager uses."""
    if not selector:
        return True
    for term in selector.split(","):
        key, _, value = term.partition("=")
        if labels.get(key.strip()) != value.strip():
            return False
    return True


def _obj(name: str, labels: dict, annotations: dict | None = None):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            labels=dict(labels),
            annotations=dict(annotations or {}),
            creation_timestamp=None,
        ),
        status=SimpleNamespace(
            active=1, succeeded=0, failed=0, start_time=None, completion_time=None
        ),
    )


class FakeBatchV1:
    def __init__(self):
        self.jobs: dict[str, SimpleNamespace] = {}
        self._seq = itertools.count(1)

    def create_namespaced_job(self, namespace, body):
        meta = body["metadata"]
        name = f"{meta['generateName']}{next(self._seq):05d}"
        job = _obj(name, meta.get("labels", {}), meta.get("annotations"))
        job.pod_labels = dict(body["spec"]["template"]["metadata"]["labels"])
        self.jobs[name] = job
        return job

    def read_namespaced_job(self, name, namespace):
        if name not in self.jobs:
            raise ApiException(status=404, reason="Not Found")
        return self.jobs[name]

    def list_namespaced_job(self, namespace, label_selector=None):
        return SimpleNamespace(
            items=[j for j in self.jobs.values() if _matches(j.metadata.labels, label_selector)]
        )

    def delete_namespaced_job(self, name, namespace, body=None):
        if name not in self.jobs:
            raise ApiException(status=404, reason="Not Found")
        del self.jobs[name]


class FakeCoreV1:
    def __init__(self, batch: FakeBatchV1):
        self.batch = batch

    def list_namespaced_pod(self, namespace, label_selector=None):
        pods = []
        for job in self.batch.jobs.values():
            labels = {
                **getattr(job, "pod_labels", job.metadata.labels),
                "job-name": job.metadata.name,
            }
            if _matches(labels, label_selector):
                pods.append(_obj(f"{job.metadata.name}-pod", labels))
        return SimpleNamespace(items=pods)

    def read_namespaced_pod_log(self, name, namespace):
        return f"SECRET LOG OF {name}"


@pytest.fixture
def k8s():
    from services.job_manager import KubernetesJobManager

    with (
        patch("services.job_manager.config.load_kube_config"),
        patch("services.job_manager.client.BatchV1Api"),
        patch("services.job_manager.client.CoreV1Api"),
    ):
        mgr = KubernetesJobManager(namespace="test-ns", in_cluster=False)
    batch = FakeBatchV1()
    mgr.batch_v1 = batch
    mgr.core_v1 = FakeCoreV1(batch)
    with patch("routers.jobs.get_job_manager", return_value=mgr):
        yield batch


@pytest.fixture
def as_tenant(app):
    """Set the VERIFIED tenant the routes see (stands in for licence introspection)."""
    import routers.jobs as jobs_router

    def _set(tenant: str) -> None:
        app.dependency_overrides[jobs_router.require_tenant] = lambda: tenant

    yield _set
    app.dependency_overrides.pop(jobs_router.require_tenant, None)


def _forged(victim: str) -> dict[str, str]:
    """A caller-supplied tenant header naming someone else. Must be ignored."""
    return {"X-Tenant-Id": victim, "X-Tenant-ID": victim}


async def _create(client, as_tenant, tenant: str, headers: dict | None = None) -> str:
    """Create a job as ``tenant``. No header by default, so the fixture's owner is
    unambiguous and a header-trusting route cannot make a spoof test pass by
    having mis-filed the victim's job at creation time."""
    as_tenant(tenant)
    resp = await client.post(
        "/api/jobs", json={"pytest_args": ["-q"], "timeout_seconds": 60}, headers=headers or {}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["job_name"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestJobsTenantScoping:
    async def test_create_labels_job_with_verified_tenant_not_header(self, client, k8s, as_tenant):
        from services.job_manager import TENANT_LABEL_KEY, tenant_label_value

        name = await _create(client, as_tenant, TENANT_A, headers=_forged(TENANT_B))
        job = k8s.jobs[name]
        assert job.metadata.labels[TENANT_LABEL_KEY] == tenant_label_value(TENANT_A)
        assert job.pod_labels[TENANT_LABEL_KEY] == tenant_label_value(TENANT_A)

    async def test_own_tenant_sees_its_own_job(self, client, k8s, as_tenant):
        name = await _create(client, as_tenant, TENANT_A)

        as_tenant(TENANT_A)
        listed = await client.get("/api/jobs", headers=_forged(TENANT_B))
        got = await client.get(f"/api/jobs/{name}", headers=_forged(TENANT_B))

        assert listed.status_code == 200
        assert listed.json()["active_jobs"] == [name]
        assert got.status_code == 200
        assert got.json()["job_name"] == name
        assert f"SECRET LOG OF {name}-pod" in got.json()["logs"]

    async def test_list_excludes_foreign_jobs(self, client, k8s, as_tenant):
        a_job = await _create(client, as_tenant, TENANT_A)
        b_job = await _create(client, as_tenant, TENANT_B)

        as_tenant(TENANT_B)
        resp = await client.get("/api/jobs", headers=_forged(TENANT_A))

        assert resp.status_code == 200
        assert resp.json()["active_jobs"] == [b_job]
        assert a_job not in resp.json()["active_jobs"]
        assert resp.json()["count"] == 1

    async def test_foreign_get_is_404_identical_to_missing(self, client, k8s, as_tenant):
        a_job = await _create(client, as_tenant, TENANT_A)

        as_tenant(TENANT_B)
        foreign = await client.get(f"/api/jobs/{a_job}", headers=_forged(TENANT_A))
        missing = await client.get("/api/jobs/pytest-job-nope", headers=_forged(TENANT_A))

        assert foreign.status_code == 404
        assert "SECRET LOG" not in foreign.text
        # No existence leak: same status, same body shape, only the echoed name differs.
        assert missing.status_code == 404
        assert foreign.text.replace(a_job, "X") == missing.text.replace("pytest-job-nope", "X")

    async def test_foreign_delete_is_404_and_job_survives(self, client, k8s, as_tenant):
        a_job = await _create(client, as_tenant, TENANT_A)

        as_tenant(TENANT_B)
        resp = await client.delete(f"/api/jobs/{a_job}", headers=_forged(TENANT_A))

        assert resp.status_code == 404
        assert a_job in k8s.jobs, "tenant B deleted tenant A's job"

        as_tenant(TENANT_A)
        own = await client.delete(f"/api/jobs/{a_job}")
        assert own.status_code == 200
        assert a_job not in k8s.jobs

    async def test_spoofed_tenant_header_is_ignored(self, client, k8s, as_tenant):
        """B presents a forged X-Tenant-Id naming A: every route still acts as B."""
        a_job = await _create(client, as_tenant, TENANT_A)

        as_tenant(TENANT_B)
        spoof = _forged(TENANT_A)
        listed = await client.get("/api/jobs", headers=spoof)
        got = await client.get(f"/api/jobs/{a_job}", headers=spoof)
        deleted = await client.delete(f"/api/jobs/{a_job}", headers=spoof)

        assert a_job not in listed.json()["active_jobs"]
        assert got.status_code == 404
        assert deleted.status_code == 404
        assert a_job in k8s.jobs

    async def test_unlabelled_job_in_namespace_is_invisible(self, client, k8s, as_tenant):
        """A pre-scoping job (or anything else in the ns) belongs to nobody."""
        k8s.jobs["legacy-job"] = _obj("legacy-job", {"app": "code-graph-helper"})

        as_tenant(TENANT_A)
        assert (await client.get("/api/jobs")).json()["active_jobs"] == []
        assert (await client.get("/api/jobs/legacy-job")).status_code == 404
        assert (await client.delete("/api/jobs/legacy-job")).status_code == 404
        assert "legacy-job" in k8s.jobs

    async def test_concurrency_cap_is_namespace_wide_and_hides_count(self, client, k8s, as_tenant):
        await _create(client, as_tenant, TENANT_A)
        await _create(client, as_tenant, TENANT_A)

        as_tenant(TENANT_B)
        with patch.dict("os.environ", {"MAX_CONCURRENT_JOBS": "2"}):
            resp = await client.post("/api/jobs", json={"pytest_args": [], "timeout_seconds": 60})

        assert resp.status_code == 429
        assert "Currently running" not in resp.text


class TestTenantLabelValue:
    def test_empty_tenant_is_refused(self):
        from services.job_manager import tenant_label_value

        for bad in ("", "   ", None):
            with pytest.raises(ValueError):
                tenant_label_value(bad)  # type: ignore[arg-type]

    def test_value_is_a_valid_label_and_distinct_per_tenant(self):
        import re

        from services.job_manager import tenant_label_value

        a, b = tenant_label_value(TENANT_A), tenant_label_value(TENANT_B)
        assert a != b
        for v in (a, b, tenant_label_value("Weird Tenant/ID with spaces" * 5)):
            assert re.fullmatch(r"[a-z0-9]{1,63}", v)
