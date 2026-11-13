"""Operation verification metadata for the aggregated OpenAPI spec.

Single source of truth for the *structural-oracle* topology of the coding API:
which operations mutate state (``producer``) versus observe it (``observer``),
and—for each producer—which observer operations can confirm its effect against
ground truth (tests pass, scan clean, file content matches, deps resolved).

This is injected into the merged OpenAPI spec by :mod:`openapi_aggregator` as
``x-operation-role`` / ``x-idempotent`` / ``x-verifies`` / ``x-verification-residue``
extensions, which survive downstream into the generated n8n node and are consumed
by the AA ``qa.verify.coding`` structural-verification subflow. No LLM judgment is
involved in verification—only deterministic predicates over these observers.

Keys are OpenAPI ``operationId`` values (snake_case, as emitted by the FastAPI
backends). The n8n node generator camelCases them downstream.

Design rule (drift-safe): the default role is derived from the HTTP method
(GET => observer, everything else => producer) so that a NEW mutating endpoint is
never silently treated as unverifiable. ``OBSERVER_OVERRIDES`` demotes the
"POST-but-read" compute endpoints (analyze/scan/validate/test/quality) back to
observers. ``PRODUCERS`` is kept explicit as well, and the test-suite asserts the
two sets are disjoint and jointly cover every non-GET operation in the live spec.
"""

from __future__ import annotations

from typing import Any

# --------------------------------------------------------------------------- #
# Producers — operations that mutate container / repo / graph / job / cache /
# service state. (Explicit; the default-by-method rule would catch new ones.)
# --------------------------------------------------------------------------- #
PRODUCERS: frozenset[str] = frozenset(
    {
        # repository workspaces: filesystem, git, exec, agent, lifecycle
        "create_workspace",
        "delete_workspace",
        "delete_all_workspaces",
        "create_workspace_directory",
        "create_workspace_file",
        "write_workspace_file",
        "move_workspace_path",
        "delete_workspace_path",
        "commit_workspace_changes",
        # typed replacements for raw `git checkout -- <f>` / `git checkout -B` execs
        "restore_workspace_files",
        "ensure_workspace_branch",
        "push_workspace_to_fork",
        # POST {ws}/{c}/git/fetch (coding-containers git_fetch.py): writes a
        # remote-tracking ref / FETCH_HEAD with a per-call repo credential. Classed
        # like its sibling push, never as a POST-but-read observer -- it changes refs.
        "fetch_workspace_ref",
        # P7 multi-repo probe (coding-containers secondary_repos.py /
        # workspace_services.py): a read-only secondary checkout under
        # /workspace/.repos, and a background service process in the workspace pod.
        "add_workspace_repo",
        "start_workspace_service",
        "stop_workspace_service",
        # POST {c}/exec/async (coding-containers #173): runs a caller command in the
        # workspace and records a durable job; same effect class as sync exec.
        "exec_in_workspace_async",
        "exec_in_workspace",
        "execute_workspace_agent",
        "install_workspace_dependencies",
        "package_upgrade_in_workspace",
        "package_modify_in_workspace",
        # dependencies
        "add_dependencies",
        "remove_dependencies",
        "replace_dependencies",
        "upgrade_in_range_packages",
        "upgrade_cross_major_packages",
        "upgrade_cves",
        # github / repositories
        "apply_code_changes",
        "import_repository_as_workspace",
        "fork_repository",
        "create_pull_request",
        "create_upstream_pull_request",
        "mark_pull_request_ready",
        "update_pull_request_body",
        # 202 + wait id (coding-github ci_wait_operations.py): registers a durable wait
        # that POSTs its callback_url once the PR head's checks finish -- a stored row
        # plus an outbound POST later, so a producer, like exec_in_workspace_async.
        "wait_for_pr_checks",
        # github issues (coding-github #82) + merge (#83)
        "create_issue",
        "comment_on_issue",
        "close_issue",
        "add_issue_labels",
        "merge_pull_request",
        "mint_internal_clone_token",
        # realtime
        "publish_broadcast",
        # code graphs
        "initialize_graph",
        "delete_graph",
        "analysis_initialize_graph",
        "analysis_delete_graph",
        # grammars
        "install_grammars",
        "install_all_grammars",
        "install_all_grammars_async",
        "cleanup_old_grammars",
        # jobs
        "create_job",
        "delete_job",
        # test orchestration / results (artifact creation)
        "create_test_suite",
        "create_orchestration_plan",
        "store_analyzed_test_result",
        # externally computed + graded result (the conveyor QA step), service accounts
        # only; writes a row attributed to its card (measured live spec 2026-10-07)
        "store_computed_test_result",
        # validation status writes
        "update_validation_status",
        "complete_validation",
        # AI service lifecycle + settings
        "start_ai_service",
        "stop_ai_service",
        "restart_ai_service",
        "update_ai_service_settings",
        # cache / metrics mutations (low-stakes)
        "clear_all_cache",
        "clear_container_cache",
        "optimize_cache",
        "update_cache_ttl",
        "warmup_container_cache",
        "reset_multi_language_metrics",
        # connection / execution cleanup
        "cleanup_test_execution",
        "cleanup_stale_connections",
    }
)

# --------------------------------------------------------------------------- #
# Observer overrides — non-GET endpoints that COMPUTE/READ ground truth without
# mutating state (analysis, scans, test execution, quality, validation, viz).
# These are exactly the "POST-but-observer" ops. ``handle_github_webhook`` /
# ``process_pr_validation`` / ``trigger_validation_webhook`` are inbound bridges
# AA never invokes as producers; treated as observers (no verification owed).
# --------------------------------------------------------------------------- #
OBSERVER_OVERRIDES: frozenset[str] = frozenset(
    {
        # one HTTP request to a workspace background service on 127.0.0.1, inside the
        # pod: it READS the provider's answer (the integration_check evidence); the
        # only state it can touch is the ephemeral in-pod provider's own.
        "probe_workspace_service",
        # analysis
        "analyze_code",
        "analyze_files_batch",
        "analyze_multi_language",
        # POST /api/analysis/symbols (coding-analysis #119): parses the code it is
        # sent and returns its definitions/imports; touches no workspace or store.
        "extract_symbols",
        "analyze_batch_complexity",
        "analyze_file_complexity",
        "analyze_codebase_semantics",
        "analyze_signatures",
        "analyze_similarity",
        "analyze_vulnerabilities",
        "analyze_code_insights",
        "detect_clones",
        "find_cross_language_patterns",
        # POST /api/analysis/contracts/call-sites (coding-analysis, the consumer side
        # of scan_contracts): reads workspace files, lists outbound HTTP/event calls.
        "scan_call_sites",
        # POST /api/analysis/contracts/routes (coding-analysis): lists the HTTP routes a
        # workspace provides; reads workspace files, mutates nothing.
        "scan_routes",
        # POST /api/analysis/structure/* (coding-analysis #130): computed structural facts
        # (module graph, public API surface, table inventory, non-HTTP entrypoints,
        # cross-repo service graph). Same shape as scan_routes / scan_call_sites: they read
        # the workspace via materialize_from_container, parse it in an isolated worker and
        # return facts; nothing is written to the workspace or any store.
        "structure_module_graph",
        "structure_api_surface",
        "structure_db_tables",
        "structure_entrypoints",
        "structure_service_graph",
        "scan_contracts",
        "scan_repository",
        "generate_call_graph",
        "generate_documentation",
        "validate_code_changes",
        "create_dependency_visualization",
        "create_visualization_graph",
        "calculate_centrality_metrics",
        "trigger_architecture_analysis",
        "architecture_cycle_check",
        "refresh_container_metrics",
        "export_multi_language_metrics",
        # bug hunter (analyses, does not fix)
        "queue_bug_hunt",
        # re-runs the detector on one finding's file AT the hunt commit (no checkout,
        # never the working tree) and reports reproduced=true|false (live spec 2026-10-07)
        "recheck_bug_hunt_finding",
        # quality (compute / report)
        "run_quality_analysis_sync",
        "trigger_quality_analysis",
        "run_quality_check",
        "run_quality_scan",
        # security (scan / report / sbom)
        "scan_container_security",
        "quick_scan_container_security",
        "scan_container_image",
        "generate_sbom",
        "scan_sbom_vulnerabilities",
        # POST /api/security/sbom/{container_name}/dependencies (coding-security #116): lists
        # each workspace repo's manifest-declared and lockfile-resolved deps with offline-OSV
        # advisory ids. Reads files (one `ls .repos/` exec); POST for the path only, writes nothing.
        "list_workspace_dependencies",
        "validate_code_security",
        "scan_container_dependencies",
        # async companions of the two scans above -- same read-compute; the
        # task_id is a transport detail, not a state change
        "scan_container_security_async",
        "scan_container_dependencies_async",
        # system dependency generators (produce text, no state change)
        "generate_ci_config",
        "generate_dockerfile_snippet",
        "generate_install_script",
        # validation (reference / consistency checks)
        "validate_file_reference",
        "validate_function_reference",
        "validate_class_reference",
        "validate_code_snippet",
        "validate_multi_line_code_snippet",
        "validate_code",
        "validate_architecture",
        "check_function_consistency",
        "check_class_consistency",
        "check_module_consistency",
        "validate_pull_request",
        # github issues / PR status — POST-but-READ. These are declared POST purely
        # because they take a request body (owner/repo/number), not because they
        # mutate anything. Without these three overrides the method default would
        # class them producers and demand a verifier for a read.
        "list_issues",
        "get_issue",
        "get_pull_request_checks",
        # PR reads (coding-github #104) — same shape: POST only for the owner/repo/
        # number body. get_pull_request returns one PR's state/merged/draft/head_sha;
        # list_pull_requests filters by state/head. Neither writes anything.
        "get_pull_request",
        "list_pull_requests",
        # coding-github #126: get_branch returns a branch's HEAD sha; compare_commits reports
        # whether base is an ancestor of head (GitHub compare status). POST for the body only.
        "get_branch",
        "compare_commits",
        # coding-github #138: compare_files returns the exact changed-file list between two refs
        # (refused when GitHub may have truncated it). POST for the body only; writes nothing.
        "compare_files",
        # Phase B reads (coding-github src/routers/github_repo_read_operations.py):
        # POST only for the owner/repo(/number) body; repository metadata, a PR's
        # changed files, Actions runs. Read-only by that module's own contract.
        "get_repository",
        # coding-github #130: one file at a resolved commit sha; POST for the body only, writes nothing.
        "get_repository_file",
        "list_pull_request_files",
        "list_workflow_runs",
        # ai architecture (ranks types for a batch passed inline; stores nothing)
        "classify_code_units",
        # grammars (check only)
        "check_grammar_updates",
        # health (batch read)
        "batch_container_health",
        # test execution (run + report; no source mutation)
        "execute_containerized_batch_tests",
        "execute_intelligent_test",
        "execute_intelligent_batch_tests",
        "execute_test_suite",
        "execute_orchestration_plan",
        "testing_execute_test",
        "testing_execute_batch_tests",
        "testing_validate_code_security",
        # py2rust A2 (coding-testing, coding-gateway#101): runs the checkout's OWN suite in
        # the workspace (pytest / cargo-nextest) and grades the JUnit report. Writes only
        # its own run config/report and may start the in-pod ws-pg; no source mutation.
        "run_workspace_tests",
        # py2rust A5b contract parity: diff a target OpenAPI document against the union of
        # source documents -- inline (pure compute) or obtained by running commands /
        # reading files in the workspace. Computes a verdict, changes nothing.
        "parity_openapi_diff",
        "parity_workspace_openapi_diff",
        # py2rust A5a schema parity: introspect two databases and diff the schemas --
        # two DSNs inside a read-only transaction, or two fresh workspace DBs migrated
        # by the caller's commands (throwaway ws-pg databases; the checkout is not
        # touched). Computes a verdict, changes nothing the caller owns.
        "parity_schema_diff",
        "parity_workspace_schema_diff",
        # py2rust A5c behaviour parity: start source/target on fresh DBs behind an
        # upstream stub and replay ordered cases; writes only its own run dir.
        "parity_behaviour_record",
        "parity_behaviour_replay",
        "parity_behaviour_compare",
        # coding-testing#89: async (202 + run_id) job modes of the workspace schema /
        # openapi diffs, and perf parity (load-test source vs target on fresh DBs behind
        # stubbed upstreams). Same verdict-only shape as the sync parity ops above; the
        # GET .../runs/{run_id} polls are observers by method default.
        "parity_workspace_schema_diff_start",
        "parity_workspace_openapi_diff_start",
        "parity_perf_start",
        # AI test insights / analysis (read-compute)
        "analyze_test_failure",
        "batch_analyze_test_failures",
        "analyze_test_insights",
        "analyze_test_impact",
        "comprehensive_test_analysis",
        "run_comprehensive_analysis",
        "predict_test_failures",
        "prioritize_tests",
        "generate_test_recommendations",
        "get_advanced_recommendations",
        "testing_analyze_test_insights",
        "testing_analyze_test_patterns",
        "testing_generate_test_recommendations",
        "testing_predict_test_failures",
        "testing_smart_test_selection",
        # inbound webhook bridges (not AA-invoked producers)
        "handle_github_webhook",
        "process_pr_validation",
        "trigger_validation_webhook",
    }
)

# --------------------------------------------------------------------------- #
# Idempotent producers — safe to re-run; informs replay-safety in the node.
# --------------------------------------------------------------------------- #
IDEMPOTENT: frozenset[str] = frozenset(
    {
        "write_workspace_file",
        "create_workspace_directory",
        "delete_graph",
        "delete_job",
        "delete_workspace",
        "clear_all_cache",
        "clear_container_cache",
        "update_validation_status",
        "complete_validation",
        "update_ai_service_settings",
        "update_cache_ttl",
        # re-adding a label the issue already carries is a 200 no-op (coding-github
        # #102, measured against the live API), so replay is safe
        "add_issue_labels",
        # switching to the branch HEAD is already on is a 200 "Already on branch"
        # (coding-containers ensure_branch docstring: "idempotent, never destructive")
        "ensure_workspace_branch",
        # re-sending the body the PR already carries is a 200 changed:false with no
        # GitHub edit (coding-github #139), so replay is safe
        "update_pull_request_body",
        # the same repository at the same ref is returned as-is (reused=true, no fetch)
        "add_workspace_repo",
        # stopping a stopped service returns its terminal state again
        "stop_workspace_service",
        # NB restore_workspace_files is deliberately NOT here: with clean_untracked a
        # replay finds the cleaned path gone and 404s ("neither at source nor on disk")
    }
)

# --------------------------------------------------------------------------- #
# Producer -> in-node verifier operation(s). Each verifier is an observer whose
# response can be asserted on (the threshold/predicate itself lives in the AA
# assert-policy asset, not here). Verifier ids must exist in the spec.
# --------------------------------------------------------------------------- #
OPERATION_VERIFIERS: dict[str, list[str]] = {
    # repository workspaces — filesystem (same container_name join key)
    "write_workspace_file": [
        "list_workspace_files",
        "get_workspace_git_status",
    ],
    "create_workspace_file": ["list_workspace_files", "get_workspace_git_status"],
    "create_workspace_directory": ["list_workspace_files"],
    "move_workspace_path": ["list_workspace_files", "get_workspace_git_status"],
    "delete_workspace_path": ["list_workspace_files", "get_workspace_git_status"],
    "commit_workspace_changes": ["get_workspace_git_status"],
    # restored/cleaned paths drop out of git status; the branch is read back as `branch`
    "restore_workspace_files": ["get_workspace_git_status"],
    "ensure_workspace_branch": ["get_workspace_git_status"],
    "exec_in_workspace": ["get_workspace_git_status", "list_workspace_files"],
    # the checkout is read back by list_workspace_repos (path, ref, head_sha)
    "add_workspace_repo": ["list_workspace_repos"],
    # state (running/exited/...) + log tail; the list shows it among the pod's services
    "start_workspace_service": ["get_workspace_service", "list_workspace_services"],
    "stop_workspace_service": ["get_workspace_service", "list_workspace_services"],
    # The wait's result (overall + per-check conclusions, or superseded/pr_closed/
    # no_checks/error) is only known at resolution: get_ci_wait_status reads it back
    # (the completion callback POSTs the same document); get_pull_request_checks reads
    # the head's checks independently of the wait store.
    "wait_for_pr_checks": ["get_ci_wait_status", "get_pull_request_checks"],
    "exec_in_workspace_async": [
        "get_exec_job_status",
        "get_workspace_git_status",
        "list_workspace_files",
    ],
    "execute_workspace_agent": [
        "get_workspace_git_status",
        "list_workspace_files",
        "run_quality_analysis_sync",
    ],
    # repository workspaces — lifecycle
    "create_workspace": [
        "get_workspace",
        "get_workspace_status",
        "get_container_readiness",
        "get_container_health",
    ],
    "delete_workspace": ["list_workspaces"],
    # dependencies
    "install_workspace_dependencies": [
        "get_workspace_dependency_status",
        "get_container_dependencies",
    ],
    "package_upgrade_in_workspace": [
        "get_container_dependencies",
        "list_outdated_packages",
        "scan_container_dependencies",
    ],
    "package_modify_in_workspace": [
        "get_container_dependencies",
        "scan_container_dependencies",
    ],
    "add_dependencies": ["get_container_dependencies", "scan_container_dependencies"],
    "remove_dependencies": ["get_container_dependencies"],
    "replace_dependencies": ["get_container_dependencies", "scan_container_dependencies"],
    "upgrade_in_range_packages": [
        "get_container_dependencies",
        "list_outdated_packages",
        "list_vulnerable_packages",
        "scan_container_dependencies",
    ],
    "upgrade_cross_major_packages": [
        "get_container_dependencies",
        "list_outdated_packages",
        "scan_container_dependencies",
    ],
    "upgrade_cves": [
        "list_vulnerable_packages",
        "scan_container_dependencies",
        "get_dependency_report",
    ],
    # github / repositories — apply_code_changes is the flagship oracle case
    "apply_code_changes": [
        "list_workspace_files",
        "validate_code_changes",
        "run_quality_analysis_sync",
        # POST /api/tests/execute. Was execute_containerized_tests, the double-mounted
        # duplicate at /api/tests/execute/execute removed by coding-testing#89.
        "testing_execute_test",
        "scan_container_security",
    ],
    "import_repository_as_workspace": [
        "get_workspace",
        "get_workspace_status",
        "get_workspace_git_status",
    ],
    "fork_repository": ["get_workspace_status", "list_workspaces"],
    "create_pull_request": ["validate_pull_request", "get_workspace_status"],
    "create_upstream_pull_request": ["validate_pull_request", "get_workspace_status"],
    # github issues — genuinely verifiable, because get_issue/list_issues read the
    # issue back. `create_issue` is confirmed by the issue existing; `close_issue` by
    # the `state` field get_issue returns. Both are real read-backs, not gestures.
    # NB list_issues EXCLUDES pull requests (GitHub models a PR as an issue), so it
    # cannot accidentally "confirm" an issue by matching an unrelated PR.
    "create_issue": ["get_issue", "list_issues"],
    "close_issue": ["get_issue"],
    # add_issue_labels (coding-github #102) is read back by get_issue, which returns
    # the issue's labels — the added label is either present afterwards or it is not.
    "add_issue_labels": ["get_issue"],
    "push_workspace_to_fork": ["get_workspace_git_status", "get_workspace_status"],
    # code graphs
    "initialize_graph": ["get_graph", "list_graphs", "analysis_get_graph_metadata"],
    "delete_graph": ["list_graphs"],
    "analysis_initialize_graph": ["analysis_get_graph_metadata", "analysis_list_graphs"],
    "analysis_delete_graph": ["analysis_list_graphs", "analysis_get_graph_metadata"],
    # grammars
    "install_grammars": ["get_grammar_status", "get_grammar_task_status"],
    "install_all_grammars": ["get_grammar_status"],
    "install_all_grammars_async": ["get_grammar_task_status", "get_grammar_status"],
    "cleanup_old_grammars": ["get_grammar_status"],
    # jobs
    "create_job": ["get_job_status", "list_jobs"],
    "delete_job": ["list_jobs"],
    # test orchestration / results
    "create_test_suite": ["list_test_suites", "get_test_suite_results"],
    "create_orchestration_plan": ["list_orchestration_history"],
    "store_analyzed_test_result": ["get_raw_test_result", "get_test_history"],
    "store_computed_test_result": ["get_raw_test_result", "get_test_history"],
    # validation status (read-back)
    "update_validation_status": ["get_validation_status"],
    "complete_validation": ["get_validation_status"],
    # AI service settings (read-back)
    "update_ai_service_settings": ["get_ai_service_settings"],
    # cache / metrics mutations (read-back; low-stakes)
    "clear_all_cache": ["get_cache_stats", "get_cache_info"],
    "clear_container_cache": ["get_container_cache_stats", "get_container_cache_info"],
    "optimize_cache": ["get_cache_stats"],
    "update_cache_ttl": ["get_cache_entry", "get_container_cache_info"],
    "warmup_container_cache": ["get_container_cache_stats"],
    "reset_multi_language_metrics": ["get_multi_language_performance"],
    # cleanup
    "cleanup_test_execution": ["list_active_executions"],
    "cleanup_stale_connections": ["get_global_health", "get_connection_health"],
}

# --------------------------------------------------------------------------- #
# Residue — producers whose in-node verification is weak or absent. AA must
# route these to an external oracle or a human/approval gate, never claim
# ground-truth verification. (reason -> consumed as x-verification-residue.)
# --------------------------------------------------------------------------- #
RESIDUE: dict[str, str] = {
    "delete_all_workspaces": (
        "destructive and irreversible; absence is observable via list_workspaces "
        "but the op must sit behind an approval gate, not auto-verification"
    ),
    "mark_pull_request_ready": (
        "draft -> ready is GitHub-side state. get_pull_request now reads `draft` back, "
        "but this op DELIBERATELY stays approval-gated with no PR-read verifier: "
        "readiness for review is a human decision, not a predicate over PR state"
    ),
    "update_pull_request_body": (
        "the PR body is GitHub-side state and get_pull_request does not read the body back, "
        "so there is no in-node verifier; the response's changed flag is the only signal"
    ),
    "merge_pull_request": (
        "merged is GitHub-side, irreversible state. get_pull_request now reads "
        "`merged` back, but this op DELIBERATELY stays approval-gated with no "
        "PR-read verifier: a read-back after the fact cannot undo a wrong merge, so "
        "the gate must sit before the call. (get_pull_request_checks reports the "
        "head commit's check verdict, not whether the PR merged.)"
    ),
    "comment_on_issue": (
        "the spec exposes no comment read: get_issue returns number/title/state/"
        "body/labels and list_issues returns the same shape, so a posted comment "
        "is unobservable here. A list-comments op would make this verifiable; "
        "until one exists, a 200 confirms acceptance, not visibility"
    ),
    "fetch_workspace_ref": (
        "the fetched commit lands under refs/remotes/<x>/ or in FETCH_HEAD, which no "
        "observer in the spec reads (get_workspace_git_status reports the checked-out "
        "branch and working-tree files only); the response's head_sha is the backend's "
        "own rev-parse, not an independent read-back"
    ),
    "mint_internal_clone_token": (
        "issues a short-lived credential; nothing observable confirms it without "
        "echoing the token, which the response deliberately does not do"
    ),
    "publish_broadcast": (
        "delivery is observable only by a WebSocket subscriber, which is not an "
        "operation in the spec; a 200 confirms acceptance, not receipt"
    ),
    "start_ai_service": "health check confirms liveness, not behavioural correctness",
    "stop_ai_service": "only absence of liveness is observable in-node",
    "restart_ai_service": "health check confirms liveness, not behavioural correctness",
}


# --------------------------------------------------------------------------- #
# Idempotent OBSERVERS — the /api/validation/* checks. They read the workspace
# and compute a verdict; re-running one with the same input against the same
# workspace returns the same answer and changes nothing, so a node may retry
# them freely. Kept separate from ``IDEMPOTENT`` (which is producers-only by
# test) because replay-safety of a READ is a different claim from a write that
# happens to be repeatable.
# --------------------------------------------------------------------------- #
IDEMPOTENT_OBSERVERS: frozenset[str] = frozenset(
    {
        "validate_file_reference",
        "validate_function_reference",
        "validate_class_reference",
        "validate_code_snippet",
        "validate_code",
        "validate_architecture",
        "check_function_consistency",
        "check_class_consistency",
        "check_module_consistency",
    }
)

# Observers that must NOT be replayed blindly, stated EXPLICITLY as
# ``x-idempotent: false``. The generator maps ``observer -> idempotent: true``
# unless the spec says otherwise, so silence here would advertise them as
# retry-safe. ``validate_multi_line_code_snippet`` accepts
# ``include_execution_validation: true`` and then RUNS the caller's code in the
# workspace -- a side effect, so a retry is not a no-op.
NON_IDEMPOTENT_OBSERVERS: frozenset[str] = frozenset(
    {
        "validate_multi_line_code_snippet",
        # sends the caller's method/body to the provider (a POST probe may change the
        # in-pod provider's state), so a retry is not guaranteed to be a no-op
        "probe_workspace_service",
        # py2rust A2/A5b: both RUN commands in the workspace (the suite / the spec
        # generator), and `async: true` submits an exec job -- a blind retry starts a
        # second run, so they are not replay-safe.
        "run_workspace_tests",
        "parity_workspace_openapi_diff",
        # py2rust A5a/A5c: RUN the caller's migrate/start commands in the workspace
        # (and create fresh ws-pg databases each call); record/replay/compare default to
        # `async: true` and submit an exec job -- a blind retry starts a second run.
        "parity_workspace_schema_diff",
        "parity_behaviour_record",
        "parity_behaviour_replay",
        "parity_behaviour_compare",
        # coding-testing#89 async starts: each submits a workspace exec job -- a blind
        # retry starts a second run.
        "parity_workspace_schema_diff_start",
        "parity_workspace_openapi_diff_start",
        "parity_perf_start",
    }
)


def role_for(operation_id: str, method: str) -> str:
    """Return ``"producer"`` or ``"observer"`` for an operation.

    Default by HTTP method (GET => observer, else producer) so new mutating
    endpoints are never silently unverifiable; ``OBSERVER_OVERRIDES`` demotes the
    POST-but-read compute ops; ``PRODUCERS`` is the explicit allow-list.
    """
    if operation_id in OBSERVER_OVERRIDES:
        return "observer"
    if operation_id in PRODUCERS:
        return "producer"
    return "observer" if method.lower() == "get" else "producer"


def annotate_operation(operation: dict[str, Any], method: str) -> None:
    """Inject x-operation-role / x-idempotent / x-verifies / x-verification-residue
    into a single OpenAPI operation object, in place. No-op without an operationId.
    """
    op_id = operation.get("operationId")
    if not isinstance(op_id, str):
        return

    role = role_for(op_id, method)
    operation["x-operation-role"] = role
    if op_id in NON_IDEMPOTENT_OBSERVERS:
        operation["x-idempotent"] = False
    elif op_id in IDEMPOTENT or op_id in IDEMPOTENT_OBSERVERS:
        operation["x-idempotent"] = True

    if role != "producer":
        return

    verifiers = OPERATION_VERIFIERS.get(op_id)
    if verifiers:
        operation["x-verifies"] = list(verifiers)
    residue = RESIDUE.get(op_id)
    if residue:
        operation["x-verification-residue"] = residue
