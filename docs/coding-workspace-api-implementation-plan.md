# Coding workspace API: evaluation and implementation plan

**Status:** accepted

**Date:** 2026-08-24 (findings re-measured against `origin/main` and the live
strato cluster 2026-08-25; §2.4 and §2.5 corrected accordingly)

**Baseline:** the workspace/repository harmonization merged in `coding-containers#105`,
`coding-client#50/#51`, `coding-github#80`, `coding-gateway#62`, and the six consumer
PRs listed in the assignment. This plan extends that design; it does not reopen the
container-versus-repository naming decision.

## 1. Decision

Implement the coding API as a hybrid owned by `coding-containers` under the canonical
`/api/workspaces` resource:

1. typed filesystem, search, and Git operations for common, auditable work;
2. structured, optimistic-concurrency-aware patch application;
3. sandboxed asynchronous process sessions for the unavoidable repository-specific
   long tail; and
4. discovered repository tasks as a convenient policy layer over process sessions.

Keep `coding-github` responsible for remote GitHub concerns: repository import,
credential brokerage, forks, pull requests, PR validation, and webhooks. Do not add new
filesystem, search, Git-worktree, or process routes under `/api/repositories` or the
legacy `/api/containers` spelling.

The proposal in the review is directionally correct, with five changes:

- Use `/api/workspaces/{container_name}/...`, not `/api/containers/...`. The persisted
  `container_name` remains the stable tenant-scoped join key during this migration, as
  decided in the harmonization work.
- Add a runtime abstraction and sandbox-hardening phase before exposing arbitrary
  processes. This is a release blocker, not optional cleanup.
- Treat process CPU, memory, and network controls as advertised runtime capabilities.
  The service must reject a requested hard control that a backend cannot enforce; it
  must never silently claim that a timeout or pod-wide limit is a per-process sandbox.
- Add a raw-content route alongside the JSON file route. A JSON response cannot cleanly
  represent both `raw` bytes and the structured UTF-8/base64 contract.
- Model task runs as process sessions. Task discovery makes commands easier to select
  and govern, but a `package.json` script or Make target is still repository-controlled
  code and is not a security boundary.

## 2. Current-state route and service analysis

### 2.1 Ownership after harmonization

| Public surface | Current implementation path | Correct long-term owner | Assessment |
|---|---|---|---|
| `/api/workspaces` | `coding-containers/src/routers/workspaces.py` republishes the existing container handlers with workspace operation IDs | `coding-containers` | Correct canonical resource; new work should be implemented here directly. |
| `/api/containers` | The same handlers, hidden from OpenAPI | Compatibility only | Retain during migration, add deprecation telemetry, and do not add new-only capabilities to it. |
| `/api/repositories/import` | `GitHubProvisioningService` validates GitHub access, resolves credentials/refs, reuses or rebuilds a workspace, then calls `/api/workspaces` | `coding-github` | Correct: this is GitHub-aware orchestration, not duplicate low-level creation. |
| Repository list/status/file/tree/directory/search | Hidden `coding-github` façades that call `/api/workspaces`, often via `/exec` | Compatibility only | Replace their internals with the typed workspace routes, then remove in a major release. |
| Fork/apply-changes/PR/validation/webhooks | PyGithub/GitHub API workflows in `coding-github` | `coding-github` | Retain. `apply-changes` is a remote delivery workflow, not a local editor. Document that distinction explicitly. |
| Gateway `/api/workspaces` and `/api/repositories` | Prefix routing plus aggregated OpenAPI | `coding-gateway` | Correct, but the HTTP proxy currently buffers whole responses and cannot carry process event streams correctly. |

### 2.2 Workspace routes and their real dependencies

| Capability | Handler/service path | How it works now | Material limitation |
|---|---|---|---|
| Create/list/get/status/delete | `routers/containers/crud_*` -> `ContainerManager` -> GitHub/Kubernetes provisioner, core Docker manager, Redis metadata registry | A façade chooses Kubernetes, agent-host, or Docker and stores `ContainerInfo` in Redis, with Kubernetes reconstruction on a registry miss | The runtime kind is inferred in several places rather than represented by one backend interface; lifecycle deletion also couples graph cleanup. |
| `mkdir`, `touch`, `write-file`, file read/list, move/delete | `routers/containers/files_*` -> `ContainerManager.execute_command` | Constructs shell commands and parses `ls`, `cat`, `find`, or command output | Path policy is inconsistent; the old read route can address paths outside the checkout, text-only reads are unbounded, `ls` text is not a stable schema, and multi-step mutations are not transactional. |
| Git status/commit/push | `git_status_commit.py`, `git_push.py` -> repeated `execute_command` calls | Parses `git status --porcelain`; commit stages files (default `files=["*"]`); push rewrites `origin`, writes a temporary credential helper, and accepts boolean force | Status loses rename/conflict/index detail; commit has an unsafe implicit staging default; multi-call operations can interleave; push needs `force-with-lease`, expected SHAs, and stronger protocol/remote policy. |
| Dependency install/status | `container_dependencies.py` -> `ContainerInstaller` | Opens Docker directly and uses `/app/data/repo`; may run `apt-get` and language package managers | It bypasses the three-tier runtime routing and is effectively Docker-only, while production workspaces are Kubernetes-backed at `/workspace`. |
| Typed package upgrade/modify | `package_upgrade.py` -> strategy registry -> `ContainerManager.execute_command` | Server constructs manager-specific commands and captures manifests before/after | Good typed pattern, but it inherits the fragmented synchronous executor, unbounded output, and weak workspace concurrency semantics. |
| Synchronous `/exec` | `container_exec.py` -> Kubernetes exec, agent-host HTTP exec, or Docker exec | Checks only the executable basename and a few joined-string patterns, defaults to user `root`, buffers output, and blocks until completion | The whitelist permits arbitrary code (`python -c`, `node -e`, package scripts/installers). It is not a security boundary and has no resumable output, cancellation, stdin, PTY, output bound, or durable process identity. |
| Agent execution | `agents.py` -> `AgentService` -> Docker/Kubernetes execution | Builds a CLI-agent command and waits for it | This should eventually use the same process-session primitive so cancellation, limits, events, and auditing do not diverge. |
| Repository search/tree façades | `coding-github/github_repository_ops.py` -> workspace `/exec` with `grep`/`find` | Parses human-readable command output and truncates at `max_results` | Limited filters and types, no resumable cursor, and duplicate policy/error handling in the wrong service. |
| Remote `apply-changes` | `coding-github/github_pull_request_operations.py` -> GitHub API | Creates/reuses a fork branch and commits whole-file changes directly to that remote branch | Useful delivery automation, but it neither edits nor verifies the local workspace. It must not substitute for patch/inspect/test/local-commit. |

### 2.3 Automation and consumer dependencies

The harmonized consumers now call the correct `/api/workspaces` prefix, but several
still depend on the weak primitives:

- `coding-analysis` has its own containers client and uses `/exec` plus whole-file reads.
- `coding-security` invokes scanners and auto-detection through `/exec`; its typed
  package upgrade/modify client is a better pattern to preserve.
- `coding-repo-jobs` runs ecosystem-specific test commands synchronously through
  `/exec` and deletes the workspace afterward.
- `coding-ai` and `coding-realtime` read through the legacy all-purpose `/files` shape.
- `coding-github` implements hidden tree/search/file compatibility routes using
  `/exec` and `/files`.
- **`CodingWorkspacesClient` has no consumers.** `coding-client#50/#51` shipped a
  22-method typed client and not one service imports it: all seven consumers are
  hand-rolled `httpx.AsyncClient` instances or the legacy `CodingContainersClient`.
  Thirteen `/exec` call sites remain, concentrated in `coding-security`'s chunked
  scan file-download (six) and `coding-github`'s tree/search façade (five). Every typed
  route added in Phases 2-6 therefore ships dead unless client adoption happens first —
  see Phase 0 step 6.
- `coding-gateway/src/operation_metadata.py` explicitly classifies workspace producers
  and observers, so every new operation needs verification metadata. As of 2026-08-25
  that metadata is enforced against the published operation set; before then the check
  was skipped unless `CODING_OPENAPI_SPEC` was set, and when first enforced it found 8
  references to operations deleted by the refactor and 9 unclassified live mutations.
- The gateway catch-all calls `httpx.AsyncClient.request`, reads
  `backend_response.content`, and returns a buffered `Response`. Its 120-second read
  timeout and 100 MiB whole-response buffer are incompatible with SSE or other
  long-lived process event streams.

### 2.4 Security findings that change the sequence

Arbitrary process execution must not ship on the present runtime profile. The findings
below are ordered by measured production reach, not by how alarming they read.

**Release blocker — the workspace pod itself.** Repository code runs as root, by
default and by request, with unbounded output:

- Workspace pods are created with an empty `securityContext`
  (`kubernetes_container_provisioner.py:853-878`), and `Dockerfile.runtime` declares no
  `USER`, so repository code runs as the image-default user, which is `uid 0`. Measured
  live on the two running `coding-jobs` pods: `sc={}`.
- The public exec request lets the caller select `user`, defaulting to `root`
  (`container_exec.py:75`).
- Output is accumulated into Python strings. A noisy command can exhaust the workspace
  pod, `coding-containers`, or the gateway.
- Package managers, interpreters, Git hooks, Make targets, and repository scripts can
  all execute arbitrary code even when their first executable is allowlisted.

**Hardening, not an escalation path — the service-account token.** Workspace pods run
as `code-graph-helper-sa` with `automountServiceAccountToken` unset, so a token is
mounted. Measured 2026-08-25: that ServiceAccount has **zero RoleBindings and zero
ClusterRoleBindings**, so the token confers no Kubernetes API authority today. Set
`automountServiceAccountToken=false` because the workload does not need it — but do not
sequence the plan as though this were a live escalation path, and do not let a future
RoleBinding be added to it without revisiting this line.

**Dev-path blocker, not a production one — Docker privileged mode.** The Docker backend
mounts `/var/run/docker.sock` and sets `privileged=True`
(`container_manager_core.py:415-416,449`), and code allowed by the existing whitelist
could then control the Docker host. That path is reachable from `docker-compose.yml`
and the agent-host backend, **not from production**: strato runs the Kubernetes backend,
and the `coding-containers` service pod is already fully hardened — measured live,
`runAsNonRoot: true`, `runAsUser/runAsGroup: 1000`, `allowPrivilegeEscalation: false`,
`capabilities.drop: [ALL]`, `readOnlyRootFilesystem: true`, `seccompProfile:
RuntimeDefault`, and no Docker socket among its volumes. Fix or fence the compose/dev
path so it does not become the template; it does not gate the production rollout.

**Duplicated backend selection.** `ContainerManager.execute_command` and
`container_exec.py` select backends with *different precedence*:
`container_exec.py:447` tests for `.svc.cluster.local` first, while
`container_manager.py:456` reaches for Kubernetes only when Docker is unavailable.
Building new operations on this layer would preserve backend-dependent behavior. (An
earlier draft of this document claimed the Docker fallback invoked `exec_run` twice.
That is not true on `origin/main` — there is exactly one call in each path,
`container_exec.py:548` and `container_manager.py:520`. The divergent precedence is the
real defect.)

These are reasons to provide a real sandbox and bounded process protocol, not reasons to
extend the executable-name whitelist.

## 3. Target API

All new routes are canonical only under:

```text
/api/workspaces/{container_name}
```

The identity remains `(tenant_id, container_name)`. New responses use
`container_name` until a separately planned identity migration. Paths inside the
checkout are POSIX paths relative to the repository root; `.` means the root. New
requests do not accept `/workspace` or arbitrary absolute paths. Compatibility routes
continue translating their historical path forms.

### 3.1 Common contract

Every new operation follows these rules:

- Pydantic models use `extra="forbid"` and discriminated unions for operations/results.
- A workspace response carries `workspace_revision`, containing at least `head_sha`,
  an index fingerprint, a worktree fingerprint, and a monotonic mutation sequence.
- Mutations accept an optional `expected_revision`; file-level mutations also accept
  expected SHA-256 values. A failed precondition returns `412`, not a generic `400`.
- `Idempotency-Key` is supported for process creation and replay-sensitive mutations.
- Paths are normalized once by a shared path-policy component. Symlinks are inspected
  with `lstat`; following is opt-in and the resolved target must remain below the
  checkout root. Device nodes, sockets, and FIFOs are rejected unless a route
  explicitly supports them.
- JSON bodies and output have endpoint-specific defaults and hard maxima. `413`
  identifies a size limit, `416` an invalid byte range, `409` a Git/worktree state
  conflict, and `501` a runtime capability the backend cannot enforce.
- Dry runs execute all parsing, path, policy, precondition, and conflict checks and
  return the same planned effect model without changing the workspace.
- Mutations are serialized by a tenant/workspace lock with a fencing token. The
  in-runtime helper also uses a checkout-local advisory lock, so calls routed to
  different `coding-containers` replicas cannot interleave.
- Secrets, file content, patches, stdin, stdout, and stderr are never written to normal
  application logs. Audit records contain identity, operation, hashes, byte counts,
  policy decision, process state, and timing.

`atomic=true` means all preconditions are validated before the first write and the
service rolls the batch back on any operation failure while holding the workspace
mutation lock. It does not claim that an unrelated program deliberately ignoring the
workspace lock cannot observe intermediate rename operations.

### 3.2 Filesystem and search

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/fs/stat` | Typed `lstat`/optional-follow metadata, SHA-256, mode, timestamps, size, MIME/binary classification, and symlink target. |
| `GET` | `/fs/file` | JSON UTF-8 or base64 read by mutually exclusive line range or byte range; returns ETag/SHA-256, totals, returned range, and truncation/cursor. |
| `GET` | `/fs/file/content` | Raw `application/octet-stream` content with HTTP `Range`, `ETag`, `If-None-Match`, and `Content-Range`. |
| `POST` | `/fs/read-batch` | Bounded multi-path reads using the same item schema; per-item errors do not erase successful items. |
| `GET` | `/fs/directory` | Paginated typed entries with exclusions, hidden-file behavior, symlink metadata, stable numeric size/mode/timestamps, and an opaque cursor. |
| `GET` | `/fs/tree` | Paginated, depth-bounded flat tree entries with include/exclude and symlink policies. Compatibility clients may assemble these into a hierarchy without forcing the server to paginate a nested object. |
| `PUT` | `/fs/file` | Atomic UTF-8/base64 create-or-replace with `if_match_sha256`, expected absence, mode, parent policy, and size bounds. |
| `POST` | `/fs/patch` | Contextual unified-diff or apply-patch-style mutation with dry run, expected file hashes, rollback, rejected-hunk detail, per-file effects, and resulting hashes. |
| `POST` | `/fs/operations` | Ordered typed `mkdir`, `touch`, `delete`, `copy`, `move`, `chmod`, `symlink`, and `truncate` operations with preconditions and rollback. |
| `POST` | `/search` | Agent-grade file/content search with typed results and revision-bound pagination. |

Patch decisions:

- Accept standard unified/Git text diffs and the existing `*** Begin Patch` form, parse
  both into one internal edit plan, and reject shelling out to an untrusted patch
  string.
- P0 rejects Git binary patch payloads. Binary changes use `PUT /fs/file` with base64;
  the response says why the patch was rejected.
- `expected_files[path]` is a SHA-256 string for an expected existing regular file or
  `null` for expected absence. With strict concurrency enabled, every path modified or
  deleted by a patch must have a precondition.
- Parse and validate every hunk, rename, mode change, and path before mutation. Return
  structured rejected hunks with old/new ranges and a reason; do not return only the
  patch program's stderr.
- Compute resulting bytes in a staging area, fsync temporary files, then replace with
  same-filesystem renames. Keep a rollback journal for the batch.

Filesystem-operation decisions:

- An operation is a discriminated object with an `op` field and only the fields valid
  for that operation.
- Recursive delete and overwrite are explicit per operation. The repository root can
  never be deleted, moved, truncated, or replaced.
- `chmod` accepts only permission bits `0000..0777`; setuid/setgid bits are rejected.
- Symlink targets are relative, and an optionally resolved target must remain within
  the checkout. Absolute or magic-link targets are rejected.
- Copy/move specify preservation and collision behavior. Directory operations report
  bounded affected-entry counts rather than returning an unbounded path list.

Search request fields:

- `roots`, `query`, `mode=literal|regex`, `result_mode=content|files|files_with_matches|count`;
- include/exclude globs, `.gitignore` policy, hidden-file policy, symlink policy;
- before/after context, case mode, word/line matching;
- maximum file bytes, binary policy, page size, scan byte/time budgets; and
- an opaque continuation cursor.

Use `ripgrep --json` (and `rg --files`) behind a typed parser, not human-readable grep
output. A cursor binds the normalized query, workspace revision, and last scan
position. If the workspace changes, continuation returns `409 cursor_stale` rather
than mixing two versions of the tree. Result models are discriminated so a content
match, file result, and count cannot be confused.

### 3.3 Git P0

| Method | Route | Key behavior |
|---|---|---|
| `GET` | `/git/status` | Parse `git status --porcelain=v2 -z --branch`; return per-path index/worktree status, rename source, conflicts, branch/upstream, ahead/behind, detached/unborn state, and revision token. |
| `GET` | `/git/diff` | Staged/unstaged/revision range, path filters, context, patch/stat/name-only modes, binary metadata, bounded bytes, and a revision-bound cursor. |
| `GET` | `/git/log` | Structured commit metadata with validated revision, path filters, ordering, and cursor pagination. |
| `GET` | `/git/show` | Structured commit/object metadata plus bounded patch or file content. |
| `GET` | `/git/refs` | Paginated local/remote branches and tags with target object IDs. |
| `GET` | `/git/remotes` | Sanitized remote names/fetch/push URLs and default-branch metadata; never credentials. |
| `POST` | `/git/branches` | Create a branch at a validated start point with expected HEAD/worktree state. |
| `POST` | `/git/switch` | Switch existing/create/detached with dirty-worktree protection; no implicit discard. |
| `POST` | `/git/stage` | Explicit paths or a validated patch/hunk generated against the expected revision. |
| `POST` | `/git/unstage` | Explicit paths or patch/hunk; does not alter worktree content. |
| `POST` | `/git/restore` | Explicit paths, source revision, and `target=index|worktree|both`; destructive variants require exact preconditions. |
| `POST` | `/git/fetch` | Allowlisted remote/protocol, bounded refs/depth, credential reference, timeout, and expected repository identity. |
| `POST` | `/git/pull` | Defaults to `ff-only`; merge/rebase behavior is never inferred. |
| `POST` | `/git/commit` | Commits the current index by default; optional explicit staging policy, expected HEAD/index, author policy, and hooks disabled unless approved. |
| `POST` | `/git/push` | Explicit refspec and remote, expected local/remote SHA, `force_with_lease` only, credential brokerage, and sanitized result. |

Implementation requirements:

- Put Git behavior in a `WorkspaceGitService`; routes must not concatenate shell
  commands. Invoke Git as argv with `--end-of-options`/`--` where supported.
- Parse NUL-delimited machine formats so spaces, newlines, renames, and non-ASCII path
  names are represented correctly.
- Disable external protocols and unsafe remote helpers. Permit only configured HTTPS/
  SSH GitHub hosts. Repository-local hooks do not run unless the request has an
  explicit approved capability.
- `commit` no longer defaults to `git add -A`. During compatibility, the old route can
  translate `files=["*"]` but emits a deprecation metric and warning.
- Replace `force: true` with `force_with_lease` and `expected_remote_sha`; reject a
  blind force.
- Resolve short-lived push/fetch credentials through `coding-github`'s internal
  credential broker. `coding-containers` owns the local Git transport but should not
  grow another long-lived GitHub credential policy.
- Network Git operations are processes with the same timeout, cancellation, output,
  network, and audit controls as other runtime work.

### 3.4 Asynchronous processes

| Method | Route | Result |
|---|---|---|
| `POST` | `/processes` | Validate policy/capabilities, create a process, and return `202` with its ID, initial state, effective limits, and links. |
| `GET` | `/processes` | Paginated processes for this workspace, filtered by state and creation time. |
| `GET` | `/processes/{process_id}` | Current state, argv summary, timestamps, exit/signal reason, effective limits, output counters/truncation, and workspace revisions before/after. |
| `GET` | `/processes/{process_id}/events` | JSON page or, with `Accept: text/event-stream`, resumable SSE after a sequence number. |
| `POST` | `/processes/{process_id}/stdin` | Bounded UTF-8/base64 input with optional EOF; conflicts if stdin is closed. |
| `POST` | `/processes/{process_id}/resize` | Change rows/columns for a running PTY process; rejected for non-PTY or terminal processes. |
| `DELETE` | `/processes/{process_id}` | Idempotent TERM/grace/KILL of the complete process group; terminal processes remain readable for their retention period. |

Create request:

```json
{
  "argv": ["pytest", "-q"],
  "cwd": ".",
  "env": {"PYTHONUNBUFFERED": "1"},
  "secret_refs": [{"env": "TOKEN", "secret_config_id": "...", "key": "TOKEN"}],
  "timeout_seconds": 900,
  "idle_timeout_seconds": 300,
  "output_limit_bytes": 10485760,
  "limits": {
    "cpu_time_seconds": 600,
    "memory_bytes": 1073741824,
    "pids": 128,
    "file_bytes": 1073741824
  },
  "network_policy": "workspace-default",
  "pty": false,
  "stdin": null,
  "workspace_access": "exclusive",
  "approval_token": null
}
```

Process rules:

- `argv` is required; no implicit shell. A caller that intentionally wants a shell must
  request an approved shell capability and pass the shell explicitly.
- The service selects the runtime user. The public request cannot choose `root`.
- Environment overrides have name/value/total-size bounds and cannot replace protected
  loader, identity, proxy, or helper variables. Secret references are resolved just in
  time, passed outside argv, redacted from the stored spec, and never returned.
- Default workspace access is exclusive. The runtime helper holds the same workspace
  lock for the process lifetime; an explicitly read-only/shared mode is enabled only
  when the backend can enforce it.
- Events have monotonic sequence numbers and distinguish `stdout`, `stderr`, and
  `system`. Payloads are UTF-8 or base64 and include stream byte offsets. Reconnect uses
  `after_seq` or SSE `Last-Event-ID`.
- Output storage is bounded. Once the retained window is full, the helper discards or
  rotates according to the declared policy, increments dropped-byte counts, and emits
  a truncation event. It never grows without limit.
- Timeout kills the process group. Idle timeout is based on no stdout/stderr/stdin
  activity. Cancellation sends TERM, waits the configured grace period, then sends
  KILL to the group.
- State is one of `queued`, `starting`, `running`, `exited`, `failed`, `cancelled`,
  `timed_out`, or `lost`. An exit code, signal, or loss reason is explicit.
- Requested hard controls are compared with runtime capabilities. Unsupported
  per-process memory/network/PTY controls return `501 capability_not_supported`; the
  response never substitutes a pod-wide setting while claiming it is per-process.
- Apply tenant/workspace concurrency quotas, process-count quotas, maximum lifetimes,
  and terminal-record retention.

The compatibility `POST /exec` remains temporarily. Its implementation becomes
"create process, wait, return the old response shape" with strict output limits. The
old whitelist can remain as a compatibility policy for that route, but documentation
must state that it is not the sandbox. New consumers use process sessions or tasks.

### 3.5 Tasks and toolchains

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/tasks` | Discover paginated typed tasks from supported manifests without executing repository code. |
| `POST` | `/tasks/{task_id}/runs` | Revalidate a task definition hash, apply task policy, and create a normal process session. |
| `GET` | `/toolchains` | Report detected manifests, installed tool versions, lockfiles, package managers, and process/runtime capabilities. |

Initial detectors cover `package.json`, Makefile, `pyproject.toml`, Cargo, Go modules,
Maven, and Gradle. Discovery parsers must not execute Make, Gradle, package scripts, or
project imports. Standard synthesized tasks include build, test, lint, format,
typecheck, check, and generate where the ecosystem defines an unambiguous command.

A task contains `task_id`, display name, category, source path/type, cwd, argv preview,
definition SHA-256, likely workspace-write/network behavior, and policy class. A run
must include the expected definition hash so a changed manifest cannot turn an
approved task into different code between discovery and execution. Task execution
returns the process-session representation and uses identical sandboxing, secrets,
limits, events, cancellation, and auditing.

The route uses a plural `runs` subresource rather than an imperative `/run`: every run
creates and returns a durable process resource that can be inspected, streamed, and
cancelled through the process API.

### 3.6 P1 capabilities

Implement only after the P0 state/precondition and process foundations are stable:

- Git merge, rebase, cherry-pick, revert, stash, blame, and remote mutation. Each
  conflict-producing operation is a state machine with `start`, `continue`, `skip`
  where valid, and `abort`; responses list typed conflicts and required next actions.
- Archive create/extract through the safe filesystem helper. Reject traversal,
  absolute paths, device files, unsafe links, decompression bombs, excessive entry
  counts, and quota overruns.
- Workspace snapshots backed by tenant-scoped object storage, not pod `/tmp`. A
  snapshot includes tracked/untracked content, modes, symlinks, Git HEAD/index, a
  manifest of hashes, encryption metadata, size/quota, and expiry. Restore uses the
  workspace mutation lock and expected revision.
- System packages as a typed environment/profile change followed by controlled
  reprovisioning, not unrestricted `apt-get` in a live root workspace. Record the
  package set and resulting runtime-image/profile digest so it is reproducible.
- Strong per-process network allowlists and CPU/memory cgroups. These require a
  process-job architecture with a shareable workspace volume or snapshot; an exec in
  the current `emptyDir` pod cannot honestly provide per-process Kubernetes network
  policy or resource limits.

## 4. Internal architecture

### 4.1 One runtime interface

Introduce a `WorkspaceRuntime` protocol selected from an explicit `runtime_kind` in
workspace metadata, with Kubernetes, Docker, and agent-host implementations:

```text
capabilities()
run_once(argv, cwd, env, stdin, timeout, output_limit, execution_profile)
start_process(process_spec)
inspect_process(process_id)
stream_process_events(process_id, after_seq)
write_process_stdin(process_id, bytes, eof)
terminate_process(process_id, grace_seconds)
```

`run_once` is for bounded, server-authored helper/Git operations. Public arbitrary work
uses `start_process`. Replace backend selection in both `ContainerManager.execute_command`
and `container_exec.py` — today they disagree on precedence (see §2.4) — and route the
dependency, package, and agent services through the same interface.
Record `runtime_kind`, opaque runtime ID, runtime API/helper version, and capabilities in
`ContainerInfo`/workspace metadata. Reconstruct sensible legacy values for existing
Kubernetes deployments.

### 4.2 In-runtime helper

Add a versioned `coding-workspace-helper` to `Dockerfile.runtime`. It accepts a bounded
JSON protocol over stdin/stdout and provides:

- safe path resolution and typed stat/read/list/mutation primitives;
- patch parsing/application and rollback journals;
- machine-readable Git invocation/parsing;
- repository search parsing;
- the shared workspace lock; and
- detached process supervision, output framing, status, stdin, PTY, and process-group
  cancellation.

Process state/output lives under a root-owned or runtime-owned bounded directory outside
the Git checkout, such as `/run/coding-workspace` or a dedicated `/tmp` mount. A process
supervisor survives an API/SSE disconnect. `events --follow --after N` emits NDJSON,
which `coding-containers` translates to SSE. This lets any service replica reconnect to
the workspace runtime instead of requiring the original HTTP worker to remain alive.

The helper protocol is versioned and capability-probed. A workspace whose image lacks a
required helper version returns a typed capability error; production does not fall back
to interpolated shell commands.

### 4.3 State, locking, and persistence

- Reuse Redis for tenant/workspace mutation leases, fencing counters, process indexes,
  idempotency keys, quotas, and bounded terminal metadata. Use Redis atomic operations;
  do not extend the in-memory fallback for correctness-sensitive process control.
- Runtime process state and bounded event files are canonical while the workspace is
  alive. If a pod is recreated, non-terminal sessions become `lost`; do not fabricate
  an exit code. Copy bounded terminal summaries to Redis for the retention window.
- The helper's workspace lock is authoritative inside the runtime. The Redis lease
  prevents duplicate admission across service replicas; fencing prevents an expired
  holder from committing after a successor acquired the lease.
- A generic process defaults to an exclusive lease for its lifetime. Reads may run
  concurrently; writes, patches, Git mutations, and other exclusive processes do not.
- Recompute the workspace revision after every mutation and when an exclusive process
  terminates. Responses include before/after revisions.

### 4.4 Runtime hardening gate

Before enabling `/processes`:

- Runtime image has an unprivileged workspace UID/GID; clone init fixes checkout
  ownership. Public processes always run as this identity.
- Kubernetes sets `runAsNonRoot`, fixed UID/GID, `allowPrivilegeEscalation=false`,
  `readOnlyRootFilesystem=true` where compatible, drops all capabilities, uses the
  runtime-default seccomp profile, and sets `automountServiceAccountToken=false`.
- Docker workspaces do not run privileged and never mount the Docker socket. If an old
  workload genuinely needs Docker, give it a separate explicitly privileged workload
  type that cannot expose the coding process API.
- Workspace-level network policy and egress proxy policy are explicit. P0 process
  requests inherit that policy; a request cannot broaden it without a short-lived
  approval/capability token.
- Approval tokens bind actor, tenant, workspace, capability, normalized request hash,
  expiry, and nonce. Validate and consume them server-side. Never treat a boolean
  `privileged=true` as approval.
- Add per-tenant process quotas and audit events before rollout.

## 5. Implementation sequence

Each step is a separately reviewable PR or tightly coupled PR set. Do not begin the next
behavioral layer until the previous acceptance gate passes on both Docker and Kubernetes
or the unsupported backend is explicitly feature-disabled.

### Phase 0 — contracts, telemetry, and compatibility inventory

Repositories: `coding-gateway`, `coding-containers`, `coding-client`.

1. Merge this plan/ADR and freeze canonical paths, operation IDs, common errors,
   precondition model, cursor rules, and limits in OpenAPI contract tests.
2. Split router assembly so existing handlers can remain mounted as hidden
   `/api/containers` aliases while new route modules mount only under
   `/api/workspaces`. Do not let the current router-reuse mechanism grow the legacy
   surface accidentally.
3. Add metrics for calls to hidden `/api/containers`, hidden repository façades, the old
   `/files`, `/exec`, implicit `git commit` staging, and blind-force push.
4. Add workspace capabilities and helper/runtime version fields without changing
   existing behavior.
5. Build a consumer matrix from gateway/service telemetry and source references; assign
   every compatibility caller an owner before announcing removal.
6. **Adopt `CodingWorkspacesClient` in all seven consumers**, replacing the hand-rolled
   `httpx.AsyncClient` instances and `CodingContainersClient` usages, with no behaviour
   change and request-shape tests pinning method, path and query parameters. This is a
   prerequisite, not cleanup: without it every typed route added later reaches consumers
   only through seven separate rewrites instead of one client bump. It also removes a
   class of defect the fleet has already paid for — `coding-realtime` sent `file_path=`
   to a route whose parameter is `path`, so FastAPI ignored it, the route listed
   `/workspace`, and every file read returned `content: None` with a 200.

Gate: OpenAPI has unique stable operation IDs, asserted fleet-wide rather than only on
merge-time collisions; legacy calls are measurable by route and calling service; no
payload contains credentials; no consumer constructs a workspace request by hand.

### Phase 1 — runtime adapter and sandbox blocker

Repositories: `coding-containers`, runtime/deployment manifests, agent-host if enabled.

1. Add the `WorkspaceRuntime` protocol and backend conformance suite.
2. Move existing synchronous execution behind `run_once`; delete duplicated backend
   selection and fix the duplicate Docker execution.
3. Convert typed package operations and agent execution to the adapter. Rewrite
   `ContainerInstaller` to be backend-neutral or make the old dependency installer a
   typed task/process workflow.
4. Add runtime metadata/capability probing and a helper-version health check.
5. Build the unprivileged runtime image (non-root `USER`, clone-init chown of the
   checkout) and set the workspace pod security context: `runAsNonRoot`, fixed
   UID/GID, `allowPrivilegeEscalation=false`, drop all capabilities, `RuntimeDefault`
   seccomp, `automountServiceAccountToken=false`. Remove `user` from the public exec
   request. Then fence the compose/dev privileged path and disable arbitrary processes
   on any legacy privileged/Docker-socket workspace.
6. Deploy workspace NetworkPolicy, quotas, approval verification, and audit scaffolding.

Gate: the same `run_once` conformance cases produce separated stdout/stderr, correct exit
status, timeout, cwd, bounded output, and tenant isolation on every enabled backend;
escape tests cannot access the Docker socket, host, service-account token, or another
workspace.

### Phase 2 — safe reads, directory listing, and search

Repositories: `coding-containers`, `coding-client`, `coding-gateway`, then
`coding-github`, `coding-ai`, `coding-analysis`, and `coding-realtime` consumers.

1. Ship helper stat/read/list and path-policy primitives.
2. Add `/fs/stat`, `/fs/file`, `/fs/file/content`, `/fs/read-batch`, `/fs/directory`,
   and `/fs/tree`.
3. Add `ripgrep` to the runtime image, the typed search service, cursors, and quotas.
4. Add typed methods to `CodingWorkspacesClient`; add gateway operation metadata and
   OpenAPI aggregation tests.
5. Make hidden GitHub file/tree/directory/search routes delegate to the new endpoints.
6. Migrate direct consumer file reads/searches; retain `/files` as a translating alias.

Gate: text, binary, large, unusual-name, symlink, ignored/hidden, paginated, stale-cursor,
and cross-tenant cases pass backend conformance and gateway end-to-end tests.

### Phase 3 — file writes, atomic operations, and patches

Repositories: `coding-containers`, `coding-client`, `coding-gateway`, editing consumers.

1. Implement the helper mutation planner, expected hashes, dry run, staging, fsync,
   rollback journal, local lock, and result models.
2. Add `PUT /fs/file`, `/fs/operations`, and `/fs/patch`.
3. Make old `mkdir`, `touch`, `write-file`, `move`, and file-delete handlers translate
   into the new service. Do not maintain two mutation implementations.
4. Add client methods, operation verification metadata, audit metrics, and gateway size
   limits appropriate to patches/binary writes.

Gate: fault injection after every planned write proves rollback; hash mismatch never
changes any target; parallel calls serialize; symlink/rename/delete traversal fuzzing
cannot escape the checkout; dry-run and actual plans agree.

### Phase 4 — inspectable local Git workflow

Repositories: `coding-containers`, `coding-client`, `coding-gateway`, `coding-github` for
credential brokerage.

1. Implement `WorkspaceGitService`, machine-format parsers, safe revision/ref/path
   validation, and bounded diff/log/show responses.
2. Ship read routes first: status, diff, log, show, refs, remotes.
3. Ship branch/switch/stage/unstage/restore with revision preconditions and lock fencing.
4. Change commit to index-only by default; keep old implicit staging only in the legacy
   translator with telemetry.
5. Add fetch/pull and credential brokerage; then replace push boolean force with lease
   semantics while translating the old request where safely possible.
6. Update gateway verification mappings so file reads/diff/status verify the correct
   producers.

Gate: rename/conflict/detached/unborn/submodule/non-ASCII cases parse correctly;
concurrent expected-HEAD tests fail safely; no Git URL, error, trace, or process argv
leaks credentials; blind force is impossible on the canonical route.

### Phase 5 — asynchronous process sessions and streaming gateway

Repositories: `coding-containers`, `coding-gateway`, `coding-client`, runtime image,
agent-host.

1. Implement helper process supervision and the runtime adapter methods.
2. Add process admission, state/index storage, idempotency, quotas, secret injection,
   capability negotiation, events, stdin, PTY where supported, and cancellation.
3. Add JSON paging and SSE on the event route.
4. Change the gateway to use `httpx` streaming plus `StreamingResponse` for SSE/raw
   streams, preserve backpressure and `Last-Event-ID`, disable the ordinary 120-second
   read timeout for approved streams, and stop buffering the whole response. Client
   disconnect closes only the event subscription; it does not cancel the process.
5. Add async process methods/event iteration to `CodingWorkspacesClient`.
6. Reimplement old `/exec` as a bounded compatibility wrapper over sessions.

Gate: reconnect resumes at the next sequence without duplication; stdout/stderr remain
separate; slow clients do not cause unbounded buffering; stdin/EOF and PTY resize (if
advertised) work; cancel/timeout kills descendants; a service or gateway restart does
not kill the workspace process; pod replacement produces honest `lost` state.

### Phase 6 — tasks and toolchains

Repositories: `coding-containers`, `coding-client`, `coding-gateway`, then
`coding-repo-jobs`, `coding-testing`, `coding-analysis`, and `coding-security`.

1. Implement non-executing manifest parsers and toolchain probes with bounded reads and
   version-command allowlists.
2. Add tasks/toolchains routes, definition hashes, policy classes, and run-to-process
   linkage.
3. Migrate repo-jobs test gates and common testing/analysis/security commands to tasks.
   Use direct processes only when no discovered/typed task represents the repository.
4. Keep typed package upgrade/modify endpoints as higher-level workflows, but run their
   commands through the process/runtime foundation.

Gate: malicious manifests cannot execute during discovery; changed task definitions
fail their expected hash; every task run has a process ID and identical event/cancel/
quota/audit behavior.

### Phase 7 — consumer migration and deprecation

Repositories: all ten harmonization repositories.

1. `coding-github`: keep only import/credential/fork/remote-delivery/PR/webhook APIs
   published; remove hidden local-operation façades after zero-use evidence.
2. `coding-analysis` and `coding-security`: replace synchronous `/exec` with tasks or
   processes; consume bounded events and explicit truncation.
3. `coding-repo-jobs` and `coding-testing`: store process/task IDs in job records and
   cancel them during job cancellation/cleanup.
4. `coding-ai` and `coding-realtime`: use typed batch/file/directory reads.
5. `coding-client`: mark container-named and old exec/files methods deprecated and
   publish a non-conflicting minor version; verify the package version is not already
   present before publish.
6. `coding-gateway`: update producer/observer/verifier/residue metadata for every new
   operation and retain old route telemetry through the removal window.

Gate: repository source search and production telemetry show no first-party calls to
hidden repository local-operation routes or legacy container paths. Announce `Sunset`
and deprecation links before a major-version removal.

### Phase 8 — P1 Git/archive/snapshot/environment work

Implement the P1 state machines and storage-backed capabilities in small PRs after P0
SLOs are met. Advanced Git comes first, archives second, snapshots/object-store third,
and reproducible system-package environment revisions last. Do not implement live root
package installation as a shortcut.

## 6. Repository-by-repository change map

| Repository | Required changes |
|---|---|
| `coding-containers` | Own all new routes/models; runtime adapter/backends; helper and runtime image; path/patch/search/Git/process/task services; Redis state/locks; sandbox policy; compatibility translators; unit/conformance/integration/security tests. |
| `coding-client` | Extend `CodingWorkspacesClient` with typed FS/search/Git/process/task methods, SSE iterator, idempotency/precondition helpers, error types, and deprecations. Keep `CodingGitHubClient` for remote workflows only. |
| `coding-gateway` | Continue prefix routing; add true streaming proxy behavior, raw range/header preservation, larger request-body policy where approved, OpenAPI tests, and complete operation verification metadata. |
| `coding-github` | Provide short-lived Git transport credentials; switch hidden local façades to canonical workspace routes; keep import/fork/delivery/PR/webhook logic; later remove hidden façades. |
| `coding-analysis` | Adopt bounded batch reads/search and process/task sessions; persist process IDs for async analyses and surface truncation/loss. |
| `coding-security` | Keep typed dependency workflows, run internals through the runtime/process adapter, and use discovered tasks/processes for scanners instead of old exec. |
| `coding-repo-jobs` | Treat test/build steps as task/process resources; persist/cancel process IDs and consume terminal states rather than one long HTTP request. |
| `coding-testing` | Use task discovery/process events for repository-defined runners; report zero-tests and truncation explicitly. |
| `coding-ai` | Use range/batch file reads and patch with expected hashes for editing; inspect diff before delivery. |
| `coding-realtime` | Use typed directory/file metadata and, only if product UI needs it, relay process events without becoming their source of truth. |

Deployment manifests outside these repositories must add workspace security contexts,
NetworkPolicy, Redis availability requirements, quotas, runtime-image rollout, and any
object-store configuration needed by P1 snapshots.

## 7. Verification strategy

### 7.1 Unit and property tests

- Pydantic discriminated models, forbidden extras, size/range limits, and stable OpenAPI.
- Path resolver fuzz/property tests for `..`, repeated separators, Unicode, control
  bytes, symlink chains/loops/races, hardlinks, long paths, and special files.
- Patch parser golden tests for create/modify/delete/rename/mode, CRLF/no-final-newline,
  overlapping/rejected hunks, expected-hash mismatch, and rollback fault injection.
- Git porcelain-v2, NUL-path, diff, log, conflict, and cursor parsers against golden
  repositories.
- Search query-to-ripgrep argv tests and JSON parser/cursor stale-state tests.
- Process state-machine tests for idempotency, sequence ordering, truncation, stdin/EOF,
  terminal races, timeout, cancel, and lost runtime.
- Task detector fixtures, including malicious manifests that would execute if imported
  or evaluated.

### 7.2 Backend conformance

Run one shared suite against Kubernetes, hardened Docker, and agent-host:

- runtime capability report;
- bounded one-shot command with separated streams and correct exit status;
- all FS/stat/read/mutation/patch semantics;
- Git inspect/mutation semantics;
- process create/status/events/stdin/cancel/descendant cleanup;
- non-root identity, cwd/env policy, output/time limits; and
- explicit rejection of a requested unsupported hard capability.

A backend is either conformant for a capability or reports it unavailable. Backend-
specific silent degradation is a test failure.

### 7.3 Security tests

- Cross-tenant BOLA tests for every route, process ID, cursor, idempotency key, and SSE
  reconnect.
- Attempts to access Docker socket/host mounts, service-account tokens, metadata
  endpoints, another checkout, and disallowed network destinations.
- Secret leakage checks across argv, process metadata, events, errors, traces, audit
  records, Git remotes/config, and gateway logs.
- Fork bombs, descendant escape/double-fork, infinite output, disk fill, idle hangs,
  decompression bombs, Git external helpers/hooks, hostile filenames, and symlink races.
- Approval-token replay, wrong workspace/tenant/request hash, expiry, and nonce races.

### 7.4 End-to-end acceptance loop

For Python, Node, Rust, Go, Java, and a mixed workspace, prove this loop only through the
gateway and `CodingWorkspacesClient`:

```text
discover tree/tasks -> search -> ranged/batch read -> dry-run patch -> apply patch
-> inspect status/diff -> run task/process -> reconnect to events -> diagnose
-> patch again -> stage -> commit -> fetch/push with lease -> create PR
```

Also prove cancellation, stale hash/HEAD, merge/conflict reporting, binary read/write,
large-output truncation, workspace deletion with a running process, service restart, and
gateway restart.

## 8. Rollout and observability

- Feature flags by tenant and backend: `workspace_fs_v2`, `workspace_git_v2`,
  `workspace_processes`, `workspace_tasks`. The process flag is impossible to enable
  when the runtime hardening capability check fails.
- Canary the runtime image and helper version, then new read routes, then mutations,
  then Git, then processes/tasks.
- Metrics: route latency/bytes, helper/runtime version, backend kind, precondition
  failures, rollback counts, cursor staleness, process admission/state/duration,
  retained/dropped output bytes, cancellations/timeouts/lost sessions, sandbox denials,
  SSE connections/backpressure, and legacy-route usage.
- Alerts: rollback failure, Redis fencing/idempotency failure, process state stuck in
  starting/terminating, output/disk quota pressure, sandbox escape indicators, secret
  redaction failure, and elevated lost-session rate.
- Do not label a feature generally available until Kubernetes conformance, security,
  gateway streaming, and the full acceptance loop pass and first-party consumers have
  migrated.

## 9. Definition of done

The implementation is complete when:

1. The canonical workspace API supports safe discover/read/search/edit/diff/verify/
   stage/commit and repository-specific processes without a Bash dependency in the
   calling agent.
2. New local capabilities exist only under `/api/workspaces`; GitHub remote workflows
   remain under `/api/repositories`.
3. No arbitrary process runs as root, in a privileged Docker container, with the Docker
   socket, or with an unnecessary Kubernetes service-account token.
4. All outputs and bodies are bounded; process output is resumable and cancellable; the
   gateway streams rather than buffers it.
5. File/Git mutations enforce revision/hash preconditions, serialize correctly, and
   either complete or roll back according to the documented atomic contract.
6. Every enabled runtime backend passes the same capability conformance and security
   suite; unsupported controls fail explicitly.
7. First-party consumers use typed routes/tasks/processes, and production telemetry
   shows no use of hidden repository local-operation façades or legacy container paths
   through the announced deprecation window.
8. Advanced Git/archive/snapshot/environment operations preserve the same tenant,
   sandbox, precondition, audit, quota, and rollback guarantees rather than bypassing
   the P0 foundation.
