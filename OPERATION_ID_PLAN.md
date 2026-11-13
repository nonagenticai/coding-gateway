# Operation ID Plan: coding-gateway

## Files to Edit

- `src/app_factory.py` (app-level routes: `/health`, `/`)
- `src/routers/health.py` (prefix `/api/health`)
- `src/routers/metrics.py` (prefix `/api/health/metrics`)
- `src/routers/cache.py` (prefix `/api/cache`)
- `src/routers/grammars.py` (prefix `/api/grammars`)
- `src/routers/graphs.py` (prefix `/api/graphs`)
- `src/routers/validation.py` (prefix `/api/validation`)
- `src/routers/validation_status.py` (prefix `/api/validation`)
- `src/routers/visualization.py` (prefix `/api/visualization`)
- `src/routers/jobs.py` (prefix `/api/jobs`)
- `src/routers/proxy.py` (catch-all, no prefix -- `include_in_schema=False`)

## Mapping Table

### app_factory.py (app-level routes, no prefix)

| Method | Effective Path | Function | operation_id |
|--------|---------------|----------|--------------|
| GET | `/health` | `root_health` | `health_check` |
| GET | `/` | `root` | `root` |

### health.py (prefix `/api/health`)

| Method | Effective Path | Function | operation_id |
|--------|---------------|----------|--------------|
| GET | `/api/health` | `health_check` | `health_api_check` |
| GET | `/api/health/ready` | `service_readiness_check` | `health_ready` |
| GET | `/api/health/live` | `service_liveness_check` | `health_live` |
| GET | `/api/health/health/ready` | `health_ready_alias` | `health_ready_alias` |
| GET | `/api/health/health/live` | `health_live_alias` | `health_live_alias` |
| GET | `/api/health/detailed` | `detailed_health_check` | `health_detailed` |
| GET | `/api/health/system-metrics` | `get_system_metrics` | `get_system_metrics` |
| GET | `/api/health/metrics` | `health_router_metrics` | `get_health_metrics` |
| GET | `/api/health/containers/unhealthy` | `get_unhealthy_containers` | `list_unhealthy_containers` |
| GET | `/api/health/container/{name}` | `get_container_health` | `get_container_health` |
| GET | `/api/health/container/{name}/history` | `get_container_health_history` | `get_container_health_history` |
| GET | `/api/health/container/{name}/ready` | `get_container_readiness` | `get_container_readiness` |
| GET | `/api/health/container/{name}/live` | `get_container_liveness` | `get_container_liveness` |
| GET | `/api/health/container/{name}/statistics` | `get_container_statistics` | `get_container_statistics` |
| POST | `/api/health/containers/batch` | `batch_container_health` | `batch_container_health` |

### metrics.py (prefix `/api/health/metrics`)

| Method | Effective Path | Function | operation_id |
|--------|---------------|----------|--------------|
| GET | `/api/health/metrics` | `metrics_index` | `metrics_index` |
| GET | `/api/health/metrics/` | `metrics_index` | `metrics_index_slash` |
| GET | `/api/health/metrics/entity/{entity_name}/metrics` | `get_entity_name_metrics` | `get_entity_name_metrics` |
| GET | `/api/health/metrics/graph-summary` | `get_graph_summary_metrics` | `get_graph_summary_metrics` |
| POST | `/api/health/metrics/centrality` | `calculate_and_store_centrality_metrics` | `calculate_centrality_metrics` |
| GET | `/api/health/metrics/entity-type/{entity_type}/metrics` | `get_entity_type_metrics` | `get_entity_type_metrics` |
| GET | `/api/health/metrics/top-entities` | `get_top_entities_by_metric` | `list_top_entities` |
| GET | `/api/health/metrics/metrics-comparison` | `compare_entity_types` | `compare_entity_types` |
| GET | `/api/health/metrics/dashboard-summary` | `get_dashboard_summary` | `get_metrics_dashboard_summary` |
| GET | `/api/health/metrics/entity-rankings` | `get_entity_rankings` | `get_entity_rankings` |
| GET | `/api/health/metrics/multi-language/performance` | `get_multi_language_performance_metrics` | `get_multi_language_performance` |
| GET | `/api/health/metrics/multi-language/languages/{language}/stats` | `get_language_performance_stats` | `get_language_performance_stats` |
| GET | `/api/health/metrics/multi-language/parser-performance` | `get_parser_performance_report` | `get_parser_performance` |
| GET | `/api/health/metrics/multi-language/top-languages` | `get_top_performing_languages` | `list_top_languages` |
| POST | `/api/health/metrics/multi-language/export` | `export_multi_language_metrics` | `export_multi_language_metrics` |
| POST | `/api/health/metrics/multi-language/reset` | `reset_multi_language_metrics` | `reset_multi_language_metrics` |
| GET | `/api/health/metrics/container/{container_name}/entity/{entity_name}/metrics` | `get_container_entity_metrics` | `get_container_entity_metrics` |
| GET | `/api/health/metrics/container/{container_name}/overview` | `get_container_overview_metrics` | `get_container_overview_metrics` |
| GET | `/api/health/metrics/container/{container_name}/health` | `get_container_health_metrics` | `get_container_health_metrics` |
| POST | `/api/health/metrics/container/{container_name}/refresh` | `refresh_container_metrics` | `refresh_container_metrics` |
| GET | `/api/health/metrics/containers/comparison` | `compare_container_metrics` | `compare_container_metrics` |

### cache.py (prefix `/api/cache`)

| Method | Effective Path | Function | operation_id |
|--------|---------------|----------|--------------|
| GET | `/api/cache/stats` | `get_cache_stats` | `get_cache_stats` |
| GET | `/api/cache/stats/{container_name}` | `get_container_cache_statistics` | `get_container_cache_stats` |
| GET | `/api/cache/info` | `get_file_cache_info` | `get_cache_info` |
| DELETE | `/api/cache/clear` | `clear_cache` | `clear_all_cache` |
| DELETE | `/api/cache/clear/{container_name}` | `clear_container_cache` | `clear_container_cache` |
| POST | `/api/cache/optimize` | `optimize_cache` | `optimize_cache` |
| GET | `/api/cache/health` | `get_cache_health` | `cache_health_check` |
| GET | `/api/cache/keys/{container_name}` | `get_container_cache_keys` | `list_container_cache_keys` |
| PATCH | `/api/cache/ttl/{container_name}/{operation}` | `update_cache_ttl` | `update_cache_ttl` |
| GET | `/api/cache/entry/{container_name}/{operation}` | `get_cache_entry_details` | `get_cache_entry` |
| GET | `/api/cache/info/{container_name}` | `get_container_cache_info` | `get_container_cache_info` |
| GET | `/api/cache/search/{container_name}` | `search_cache_keys` | `search_cache_keys` |
| GET | `/api/cache/export/{container_name}` | `export_container_cache` | `export_container_cache` |
| POST | `/api/cache/warmup/{container_name}` | `warmup_cache` | `warmup_container_cache` |
| GET | `/api/cache/compare` | `compare_container_caches` | `compare_container_caches` |
| GET | `/api/cache/evictions/{container_name}` | `get_cache_eviction_history` | `get_cache_eviction_history` |

### grammars.py (prefix `/api/grammars`)

| Method | Effective Path | Function | operation_id |
|--------|---------------|----------|--------------|
| GET | `/api/grammars/status` | `get_grammar_status` | `get_grammar_status` |
| POST | `/api/grammars/install` | `install_grammars` | `install_grammars` |
| POST | `/api/grammars/install-all` | `install_all_grammars` | `install_all_grammars` |
| POST | `/api/grammars/install-all-async` | `install_all_grammars_async` | `install_all_grammars_async` |
| GET | `/api/grammars/tasks/{task_id}` | `check_task_status` | `get_grammar_task_status` |
| POST | `/api/grammars/check-updates` | `check_grammar_updates` | `check_grammar_updates` |
| DELETE | `/api/grammars/cleanup` | `cleanup_old_grammars` | `cleanup_old_grammars` |
| GET | `/api/grammars/supported-languages` | `get_supported_languages` | `list_grammar_supported_languages` |

### graphs.py (prefix `/api/graphs`)

| Method | Effective Path | Function | operation_id |
|--------|---------------|----------|--------------|
| POST | `/api/graphs/initialize` | `initialize_code_graph` | `initialize_graph` |
| DELETE | `/api/graphs/{container_name}` | `delete_graph` | `delete_graph` |
| DELETE | `/api/graphs/cleanup/orphaned` | `cleanup_orphaned_graphs` | `cleanup_orphaned_graphs` |
| GET | `/api/graphs` | `list_graphs` | `list_graphs` |
| GET | `/api/graphs/` | `list_graphs` | `list_graphs_slash` |
| GET | `/api/graphs/initialize-disabled-get` | `initialize_code_graph_get` | `initialize_graph_disabled_get` |
| GET | `/api/graphs/{container_name}` | `get_graph_details` | `get_graph` |
| GET | `/api/graphs/{container_name}/nodes` | `get_graph_nodes` | `list_graph_nodes` |
| GET | `/api/graphs/{container_name}/edges` | `get_graph_edges` | `list_graph_edges` |
| GET | `/api/graphs/{container_name}/ai-augmented` | `get_augmented_graph` | `get_ai_augmented_graph` |
| GET | `/api/graphs/{container_name}/runtime-augmented` | `get_runtime_augmented_graph` | `get_runtime_augmented_graph` |
| GET | `/api/graphs/{container_name}/augmented` | `get_architecture_code_graph` | `get_architecture_graph` |
| GET | `/api/graphs/internal/database/counts/{container_name}` | `get_internal_database_counts` | `get_internal_database_counts` |
| GET | `/api/graphs/debug/database/verify/{container_name}` | `debug_database_verification` | `debug_database_verify` |

### validation.py (prefix `/api/validation`)

| Method | Effective Path | Function | operation_id |
|--------|---------------|----------|--------------|
| POST | `/api/validation/file-reference` | `validate_file_reference` | `validate_file_reference` |
| POST | `/api/validation/function-reference` | `validate_function_reference` | `validate_function_reference` |
| POST | `/api/validation/class-reference` | `validate_class_reference` | `validate_class_reference` |
| POST | `/api/validation/code-snippet` | `validate_code_snippet` | `validate_code_snippet` |
| POST | `/api/validation/function-consistency` | `check_function_description_consistency` | `check_function_consistency` |
| POST | `/api/validation/class-consistency` | `check_class_description_consistency` | `check_class_consistency` |
| POST | `/api/validation/module-consistency` | `check_module_consistency` | `check_module_consistency` |
| POST | `/api/validation/code` | `validate_code` | `validate_code` |
| POST | `/api/validation/architecture` | `validate_architecture` | `validate_architecture` |

### validation_status.py (prefix `/api/validation`)

| Method | Effective Path | Function | operation_id |
|--------|---------------|----------|--------------|
| POST | `/api/validation/status` | `update_validation_status` | `update_validation_status` |
| POST | `/api/validation/complete` | `complete_validation` | `complete_validation` |
| GET | `/api/validation/{validation_id}` | `get_validation_status` | `get_validation_status` |

### visualization.py (prefix `/api/visualization`)

| Method | Effective Path | Function | operation_id |
|--------|---------------|----------|--------------|
| POST | `/api/visualization/create-graph` | `create_dependency_visualization` | `create_visualization_graph` |
| GET | `/api/visualization/capabilities` | `get_visualization_capabilities` | `get_visualization_capabilities` |
| GET | `/api/visualization/examples` | `get_visualization_examples` | `get_visualization_examples` |

### jobs.py (prefix `/api/jobs`)

| Method | Effective Path | Function | operation_id |
|--------|---------------|----------|--------------|
| POST | `/api/jobs` | `create_job` | `create_job` |
| POST | `/api/jobs/` | `create_job` | `create_job_slash` |
| GET | `/api/jobs/health` | `health_check` | `jobs_health_check` |
| GET | `/api/jobs/{job_name}` | `get_job_status` | `get_job_status` |
| DELETE | `/api/jobs/{job_name}` | `delete_job` | `delete_job` |
| GET | `/api/jobs` | `list_active_jobs` | `list_jobs` |
| GET | `/api/jobs/` | `list_active_jobs` | `list_jobs_slash` |

### proxy.py (catch-all, no prefix, `include_in_schema=False`)

| Method | Effective Path | Function | operation_id |
|--------|---------------|----------|--------------|
| * | `/{path:path}` | `proxy_request` | `proxy_request` |
| WS | `/api/realtime/ws/{path:path}` | `ws_realtime_proxy` | (websocket, no operation_id needed) |
| WS | `/api/broadcast/subscribe` | `ws_broadcast_subscribe_proxy` | (websocket, no operation_id needed) |

**Note:** The proxy catch-all has `include_in_schema=False` and uses `api_route` with multiple methods. WebSocket endpoints do not use operation_ids. The proxy route still benefits from an explicit `operation_id` for internal reference.

## Total: 98 routes (including slash duplicates and proxy, excluding websockets)

## Execution Steps

```bash
cd /Users/mini1/Movies/code_graph_helper/coding-gateway && git pull --rebase
```

1. Add `operation_id=` to every `@router.get/post/put/delete/patch(` decorator, `@router.api_route(` decorator, and `@app.get(` decorator in all files listed above
2. For `include_in_schema=False` duplicate routes (slash variants), assign a unique `operation_id` with `_slash` suffix
3. Validate uniqueness -- ensure no duplicate `operation_id` values across the entire app
4. Run pre-commit checks:
   ```bash
   pre-commit run --all-files
   ```
5. Run tests:
   ```bash
   pytest
   ```
6. Commit and push:
   ```bash
   git add -A && git commit -m "Add explicit operation_id to all route decorators" && git push
   ```
