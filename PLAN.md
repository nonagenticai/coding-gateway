# coding-gateway — Extraction Plan

## Purpose
API Gateway that keeps the current port 8687. Handles routing to backend services, plus owns health, metrics, cache, grammars, visualization, validation, graphs, and jobs endpoints directly.

## Port: 8687

---

## Endpoints Owned Directly

| Route Prefix | Source Router | Source Endpoint File |
|---|---|---|
| `/api/health` | `health` | `src/app/api/endpoints/health.py` |
| `/api/health/metrics` | `metrics` | `src/app/api/endpoints/metrics.py` |
| `/api/cache` | `cache` | `src/app/api/endpoints/cache.py` |
| `/api/grammars` | `grammars` | `src/app/api/endpoints/grammars.py` |
| `/api/visualization` | `visualization` | `src/app/api/endpoints/visualization.py` |
| `/api/validation` | `validation` | `src/app/api/endpoints/validation.py` |
| `/api/validation` (status) | `validation_status` | `src/app/api/endpoints/validation_status.py` |
| `/api/graphs` | `graphs` | `src/app/api/endpoints/graphs.py` |
| `/api/jobs` | `jobs` | `src/app/api/endpoints/jobs.py` |

## Endpoints Proxied to Backend Services

| Route Prefix | Target Service |
|---|---|
| `/api/analysis/*` | coding-analysis:8688 |
| `/api/bug-hunter/*` | coding-analysis:8688 |
| `/api/quality/*` | coding-analysis:8688 |
| `/api/tests/*` | coding-testing:8689 |
| `/api/ai/*` | coding-ai:8690 |
| `/api/containers/*` | coding-containers:8691 |
| `/api/repositories/*` | coding-github:8692 |
| `/api/webhooks/pr-actions/*` | coding-github:8692 |
| `/api/security/*` | coding-security:8693 |
| `/api/dependencies/*` | coding-security:8693 |
| `/api/ws-health/*` | coding-realtime:8694 |
| `/api/realtime/*` | coding-realtime:8694 |
| `/api/broadcast/*` | coding-realtime:8694 |
| `/api` (ws rate limits) | coding-realtime:8694 |

## Services to Extract

| Monolith Service | Target Location |
|---|---|
| `app_factory.py` | `src/main.py` (app creation) |
| `middleware/*` | `src/` (auth from coding-client, custom middleware) |
| `cache_manager.py` | `src/services/cache_manager.py` |
| `services/code_graph/` (12 files) | `src/services/code_graph/` |
| `services/code_graph_metrics*.py` | `src/services/code_graph_metrics.py` |
| `services/visualization_service.py` | `src/services/visualization.py` |
| `services/validation_service.py` | `src/services/validation.py` |
| `services/schema_version_service.py` | `src/services/schema_version.py` |

## DB Tables Owned
- SchemaVersion
- NodeAudit
- Graph-related tables (Node, Edge via code_graph services)

## Dependencies
- coding-client (shared models, auth, DB)
- httpx (for proxying to backend services)
- redis (caching)
