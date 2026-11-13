"""Tests for the structural-oracle operation metadata (operation_metadata.py).

Offline tests guard internal consistency (disjoint role sets, verifier coverage,
annotation behaviour). The spec-drift tests validate every referenced operationId
against the fleet's published operation set.

That set comes from ``tests/data/coding_operation_index.json`` -- a vendored
index of ``(method, path, operationId)`` refreshed by
``scripts/refresh_operation_index.py``. It is a fixture, not a lockfile: the
gateway cannot compute the aggregated spec on its own, because it is assembled
from every backend at runtime. ``CODING_OPENAPI_SPEC`` still overrides it with a
full spec, which is how you check this metadata against a live gateway.

These tests ran only under that env var until 2026-08-25, so nothing enforced
them in CI. When first enforced they found 8 metadata references to operations
deleted in the 2026-08-24 container->workspace refactor, and 9 live non-GET
operations that had never been classified at all.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import operation_metadata as om


# --------------------------------------------------------------------------- #
# Internal consistency
# --------------------------------------------------------------------------- #
def test_producers_and_observer_overrides_are_disjoint():
    overlap = om.PRODUCERS & om.OBSERVER_OVERRIDES
    assert not overlap, f"ops classified as both producer and observer: {sorted(overlap)}"


def test_every_verifier_key_is_a_producer():
    stray = set(om.OPERATION_VERIFIERS) - om.PRODUCERS
    assert not stray, f"OPERATION_VERIFIERS keys that are not producers: {sorted(stray)}"


def test_every_producer_is_covered_by_verifiers_or_residue():
    covered = set(om.OPERATION_VERIFIERS) | set(om.RESIDUE)
    uncovered = om.PRODUCERS - covered
    assert not uncovered, (
        "producers with neither a verifier nor a residue reason "
        f"(would be silently unverified): {sorted(uncovered)}"
    )


def test_idempotent_ops_are_producers():
    stray = om.IDEMPOTENT - om.PRODUCERS
    assert not stray, f"idempotent flags on non-producers: {sorted(stray)}"


def test_verifiers_are_non_empty_lists():
    for producer, verifiers in om.OPERATION_VERIFIERS.items():
        assert verifiers, f"{producer} has an empty verifier list"
        assert len(set(verifiers)) == len(verifiers), f"{producer} has duplicate verifiers"


# --------------------------------------------------------------------------- #
# Role classification
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("op_id", "method", "expected"),
    [
        ("write_workspace_file", "post", "producer"),
        ("import_repository_as_workspace", "post", "producer"),
        ("apply_code_changes", "post", "producer"),
        ("delete_workspace", "delete", "producer"),
        ("upgrade_in_range_packages", "post", "producer"),
        # POST-but-observer compute ops must NOT be producers
        ("analyze_code", "post", "observer"),
        ("run_quality_analysis_sync", "post", "observer"),
        ("testing_execute_test", "post", "observer"),
        ("scan_container_security", "post", "observer"),
        ("validate_pull_request", "post", "observer"),
        ("get_pull_request", "post", "observer"),
        ("list_pull_requests", "post", "observer"),
        ("get_branch", "post", "observer"),
        ("compare_commits", "post", "observer"),
        ("compare_files", "post", "observer"),
        ("add_issue_labels", "post", "producer"),
        # plain reads
        ("list_workspace_files", "get", "observer"),
        ("get_workspace_git_status", "get", "observer"),
        # unknown non-GET defaults to producer (drift-safe)
        ("some_new_future_mutation", "post", "producer"),
        ("some_new_future_read", "get", "observer"),
    ],
)
def test_role_for(op_id: str, method: str, expected: str):
    assert om.role_for(op_id, method) == expected


# --------------------------------------------------------------------------- #
# Annotation
# --------------------------------------------------------------------------- #
def test_annotate_producer_with_verifiers():
    op = {"operationId": "apply_code_changes"}
    om.annotate_operation(op, "post")
    assert op["x-operation-role"] == "producer"
    assert "testing_execute_test" in op["x-verifies"]
    assert "scan_container_security" in op["x-verifies"]


def test_annotate_observer_has_no_verifies():
    op = {"operationId": "run_quality_analysis_sync"}
    om.annotate_operation(op, "post")
    assert op["x-operation-role"] == "observer"
    assert "x-verifies" not in op


def test_annotate_residue_producer():
    op = {"operationId": "delete_all_workspaces"}
    om.annotate_operation(op, "delete")
    assert op["x-operation-role"] == "producer"
    assert "x-verification-residue" in op


def test_annotate_idempotent_flag():
    op = {"operationId": "write_workspace_file"}
    om.annotate_operation(op, "post")
    assert op.get("x-idempotent") is True


def test_annotate_no_operation_id_is_noop():
    op: dict = {}
    om.annotate_operation(op, "post")
    assert op == {}


# --------------------------------------------------------------------------- #
# Opt-in: validate referenced ids against a real spec
# --------------------------------------------------------------------------- #
def _all_operation_ids(spec: dict) -> set[str]:
    methods = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}
    ids: set[str] = set()
    for path_item in (spec.get("paths") or {}).values():
        if not isinstance(path_item, dict):
            continue
        for method, op in path_item.items():
            if (
                method in methods
                and isinstance(op, dict)
                and isinstance(op.get("operationId"), str)
            ):
                ids.add(op["operationId"])
    return ids


_INDEX_PATH = Path(__file__).resolve().parent.parent / "data" / "coding_operation_index.json"


def _published_operations() -> list[tuple[str, str, str]]:
    """Return ``(method, path, operationId)`` for every operation the fleet publishes.

    Prefers a full spec at ``CODING_OPENAPI_SPEC`` (point it at a live gateway's
    ``openapi.json`` to check this metadata against production); otherwise reads
    the vendored index so the drift tests always run.
    """
    override = os.getenv("CODING_OPENAPI_SPEC")
    if override:
        with open(override) as fh:
            spec = json.load(fh)
        methods = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}
        return [
            (method, path, op["operationId"])
            for path, item in (spec.get("paths") or {}).items()
            if isinstance(item, dict)
            for method, op in item.items()
            if method in methods and isinstance(op, dict) and isinstance(op.get("operationId"), str)
        ]
    with open(_INDEX_PATH) as fh:
        index = json.load(fh)
    return [(o["method"], o["path"], o["operationId"]) for o in index["operations"]]


def test_referenced_ids_exist_in_spec():
    published = _published_operations()
    known = {op_id for _, _, op_id in published}

    referenced = set(om.PRODUCERS) | set(om.OBSERVER_OVERRIDES)
    for verifiers in om.OPERATION_VERIFIERS.values():
        referenced.update(verifiers)

    missing = referenced - known
    assert not missing, (
        f"metadata references operationIds absent from the published spec: {sorted(missing)}. "
        "Either the operation was removed (drop the metadata) or the vendored index is "
        "stale (re-run scripts/refresh_operation_index.py)."
    )

    # Every non-GET op in the spec must be classified (drift detection).
    non_get = {op_id for method, _, op_id in published if method != "get"}
    unclassified = non_get - om.PRODUCERS - om.OBSERVER_OVERRIDES
    assert not unclassified, (
        f"non-GET ops not classified producer/observer: {sorted(unclassified)}. "
        "An unclassified mutation defaults to producer with no verifier and no residue, "
        "so AA cannot tell 'verified' from 'unverifiable'."
    )


def test_published_operation_ids_are_unique_across_the_fleet():
    """One operationId, one operation -- fleet-wide.

    The whole metadata scheme keys on operationId, and so does the generated n8n
    node. Two backends emitting the same id silently collapse into one node
    operation whose route depends on merge order. ``openapi_aggregator`` prefixes
    a collision it detects at merge time, but nothing asserted that the published
    result is actually collision-free.
    """
    published = _published_operations()
    seen: dict[str, list[str]] = {}
    for method, path, op_id in published:
        seen.setdefault(op_id, []).append(f"{method.upper()} {path}")
    duplicates = {op_id: routes for op_id, routes in seen.items() if len(routes) > 1}
    assert not duplicates, f"operationIds published by more than one route: {duplicates}"


def test_operation_index_fixture_is_self_consistent():
    """Negative control: prove the fixture is actually populated.

    Every assertion above passes vacuously against an empty operation list -- an
    index that failed to generate would look like a clean bill of health.
    """
    with open(_INDEX_PATH) as fh:
        index = json.load(fh)
    assert index["operation_count"] == len(index["operations"])
    assert index["operation_count"] > 250, "index looks truncated; regenerate it"
    assert {"method", "path", "operationId"} == set(index["operations"][0])


def test_operation_index_is_sorted_like_the_refresh_script():
    """The vendored index must be ordered exactly as scripts/refresh_operation_index.py writes it
    (operationId, method, path), so a hand-added entry cannot drift from a regenerated file."""
    with open(_INDEX_PATH) as fh:
        ops = json.load(fh)["operations"]
    keys = [(o["operationId"], o["method"], o["path"]) for o in ops]
    assert keys == sorted(keys), (
        "coding_operation_index.json is not sorted by (operationId, method, path)"
    )


@pytest.mark.parametrize("op_id", sorted(om.IDEMPOTENT_OBSERVERS))
def test_validation_ops_are_idempotent_observers(op_id):
    """The /api/validation/* checks are reads: safe to replay, flagged as such."""
    op = {"operationId": op_id}
    om.annotate_operation(op, "post")
    assert op["x-operation-role"] == "observer"
    assert op.get("x-idempotent") is True
    assert "x-verifies" not in op


def test_idempotent_observers_are_observers_not_producers():
    assert om.IDEMPOTENT_OBSERVERS <= om.OBSERVER_OVERRIDES
    assert not (om.IDEMPOTENT_OBSERVERS & om.PRODUCERS)


def test_a_non_idempotent_observer_gets_no_flag():
    """Negative control: the flag is per-op, not 'every observer'."""
    op = {"operationId": "run_quality_analysis_sync"}
    om.annotate_operation(op, "post")
    assert "x-idempotent" not in op


def test_executing_snippet_validator_is_explicitly_not_idempotent():
    """validate_multi_line_code_snippet can RUN code (include_execution_validation):
    it must carry an explicit x-idempotent: false, never be absent (absent reads as
    'observer => idempotent' downstream) and never true."""
    op = {"operationId": "validate_multi_line_code_snippet"}
    om.annotate_operation(op, "post")
    assert op["x-operation-role"] == "observer"
    assert op["x-idempotent"] is False
    assert "validate_multi_line_code_snippet" not in om.IDEMPOTENT_OBSERVERS


def test_the_other_nine_validation_observers_stay_idempotent():
    """Positive control: exactly nine validation observers keep x-idempotent: true."""
    assert len(om.IDEMPOTENT_OBSERVERS) == 9
    assert not (om.IDEMPOTENT_OBSERVERS & om.NON_IDEMPOTENT_OBSERVERS)
    op = {"operationId": "validate_code_snippet"}
    om.annotate_operation(op, "post")
    assert op["x-idempotent"] is True
