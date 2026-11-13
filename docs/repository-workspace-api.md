# Repository workspace API consolidation

## Decision

The public resource is a **workspace**: a checked-out GitHub repository plus an
isolated execution runtime. The runtime can be a Docker container, Kubernetes
Deployment/pod, or agent-host process, so `container` is not a stable domain
name. A GitHub repository is the remote provider resource and is not the same
thing as its mutable local checkout.

Ownership is split by capability:

| Owner | Canonical responsibility |
|---|---|
| `coding-containers` | `/api/workspaces`: provision/list/status/delete, local files, local git, exec, agents, dependency changes |
| `coding-github` | `/api/repositories`: GitHub-aware workspace import, remote forks, commits to forks, pull requests, PR validation, webhooks, clone credentials |
| `coding-gateway` | Routes both prefixes and aggregates their OpenAPI documents |

`container_name` remains the stable `(tenant_id, container_name)` join key in
payloads and path parameters during this migration. Renaming persisted identity
would add database and event migration risk without removing route duplication.

## Route analysis

Before consolidation, two public paths represented the same workspace:

| Duplicated capability | Container route | Repository façade | Actual owner |
|---|---|---|---|
| Direct create | `POST /api/containers/` | n/a | `coding-containers` |
| GitHub-aware import | n/a | `POST /api/repositories/provision` | `coding-github` orchestration, delegating creation to `coding-containers` |
| List | `GET /api/containers/` | `GET /api/repositories/repositories` | `coding-containers` |
| Status | `GET /api/containers/{name}/status` | `GET /api/repositories/status/{name}` | `coding-containers` |
| Read/list files | `GET /api/containers/{name}/files` | `GET .../{name}/file` and `/directory` | `coding-containers` |
| Traverse/search | `POST .../{name}/exec` | `GET .../{name}/tree`, `POST .../{name}/search` | `coding-containers` runtime |

The list/status/file/tree/directory/search repository façades used
`_ContainersHTTPAdapter` or direct HTTP calls back to the containers service.
Some did not forward the tenant/service identity, so valid tenant-scoped calls
could fail and the ownership boundary was inconsistently enforced.

Provisioning required a separate conclusion after inspecting its service path:
`GitHubProvisioningService` performs GitHub access validation, secret
resolution, branch/PR commit lookup, idempotent reuse or force-rebuild, and
optional indexing before delegating workspace creation. That is not equivalent
to low-level workspace creation. Its canonical spelling is therefore
`POST /api/repositories/import`; the older `/provision` spelling is hidden but
callable.

Remote fork/PR/webhook routes do not overlap container lifecycle and remain in
`coding-github`. `coding-repo-jobs` already follows this boundary: it calls the
workspace owner for create/status/exec/delete and calls `coding-github` for PR
publication.

## Migration behavior

- `/api/workspaces` is published in OpenAPI with workspace operation IDs.
- `/api/containers` remains callable with identical handlers and responses but
  is hidden from OpenAPI.
- `POST /api/repositories/import` publishes the GitHub-aware import workflow;
  the historical `/provision` spelling remains a hidden callable alias.
- The historical repository list/status/file/tree/directory/search routes
  remain callable but are hidden from OpenAPI and delegate to canonical
  workspace paths with tenant and service identity forwarded.
- Remote `/api/repositories` operations remain published.
- `CodingWorkspacesClient` is the canonical service client.

This is an additive compatibility release. Legacy route removal should happen
only in a separately announced major version after traffic confirms migration.

## Follow-on implementation

The source-backed evaluation and phased plan for filesystem, patch, search, Git,
process-session, task, archive, snapshot, and environment capabilities is in
[coding-workspace-api-implementation-plan.md](coding-workspace-api-implementation-plan.md).
