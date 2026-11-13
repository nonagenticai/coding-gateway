# coding-gateway

API gateway for the Code Graph Helper microservices platform. Routes requests to seven backend services while owning graph storage, code validation, cache management, grammar lifecycle, visualization, metrics, and Kubernetes job orchestration directly.

## What Code Graph Helper Does

Code Graph Helper is a platform that turns source code into structured, queryable knowledge graphs. It solves the problem of understanding large, multi-language codebases at scale — how functions call each other, which classes inherit from what, where security vulnerabilities hide, and how architecture actually looks vs. how it was designed.

**The pipeline:**

1. **Ingest** — A GitHub repo is forked or cloned into an isolated Docker container (`coding-containers`). The repo's webhook events (push, PR, comment) are captured by `coding-github`.
2. **Parse** — Tree-sitter grammars parse source files across 9 languages into ASTs. The gateway extracts entities (functions, classes, modules) and relationships (calls, imports, inherits) into a per-repo code graph stored in SQLite.
3. **Analyze** — `coding-analysis` runs static analysis, complexity metrics, architecture extraction, semantic cross-language relationship detection, clone detection, bug hunting, and quality scanning on the codebase inside the container.
4. **Secure** — `coding-security` scans for vulnerabilities (SQL injection, XSS, command injection, hardcoded secrets), checks package dependencies against OSV.dev for CVEs, and generates sandbox configurations.
5. **Test** — `coding-testing` orchestrates test execution across 7 language-specific executors (Python, JavaScript, Java, Rust, C#, Go, C++) running inside the container, manages test suites, and stores results.
6. **Reason** — `coding-ai` uses PydanticAI agents (OpenAI, Anthropic, Groq) to generate code insights, analyze test failures with fix suggestions, and predict test suite health.
7. **Stream** — `coding-realtime` pushes live parse results and analysis events to IDE clients over WebSocket.

All services authenticate via Istio-forwarded headers (`x-user-id`, `x-tenant-id`) with trusted-network bypass for internal traffic. External users get license tokens validated via `licenses.licenses.svc.cluster.local:8544/license/introspect`.

### Service Map

| Service | Port | Description |
|---------|------|-------------|
| **coding-gateway** | 8687 | API gateway — reverse proxy + graph CRUD, validation, cache, grammars, visualization, metrics, K8s jobs |
| **coding-analysis** | 8688 | Multi-language static analysis engine — AST parsing, complexity metrics, architecture extraction, semantic relationships, clone detection, bug hunting, quality scanning via tree-sitter (9 languages) |
| **coding-testing** | 8689 | Test orchestration — dispatches test runs to 7 language executors in Docker containers, manages suites/plans, stores results, AI-powered failure analysis and prediction |
| **coding-ai** | 8690 | AI-powered insights — code quality analysis, test failure root-cause analysis with fix suggestions, test suite health prediction using PydanticAI agents |
| **coding-containers** | 8691 | Container lifecycle — provisions Docker/K8s containers for GitHub repos, file operations, git workflows, CLI agent execution (Claude Code, Codex), dependency installation |
| **coding-github** | 8692 | GitHub integration — dual-token fork/PR workflows, webhook processing (push, PR, comments), automated PR validation for security/performance/quality |
| **coding-security** | 8693 | Security analysis — multi-language vulnerability scanning, package dependency CVE detection (OSV.dev), system dependency management, sandbox config generation |
| **coding-realtime** | 8694 | WebSocket gateway — real-time analysis streaming to IDE clients, broadcast pub/sub, connection health monitoring, rate limiting |
| **python-test-executor** | 8087 | Executes Python tests in subprocesses with timeout enforcement and syntax validation |
| **javascript-test-executor** | 8085 | Runs Jest tests in isolated temp directories, returns structured results |
| **java-test-executor** | 8088 | Compiles and runs Java tests (JUnit/TestNG) with Maven/Gradle, JaCoCo coverage, Checkstyle/PMD/SpotBugs |
| **rust-test-executor** | 8089 | Runs Cargo tests, tarpaulin coverage, clippy analysis, cargo-audit security scanning |
| **csharp-test-executor** | 8090 | Executes .NET tests via dotnet CLI with coverage (coverlet) and security scanning |
| **go-test-executor** | 8091 | Runs Go tests with race detection, coverage, and vet analysis |
| **cpp-test-executor** | 8094 | Builds and runs C++ tests (Google Test, Catch2) with CMake, coverage (gcov), cppcheck, valgrind |

## What This Service Does

Code Graph Helper is a platform for building, querying, and analyzing code graphs -- directed graphs where nodes represent code entities (functions, classes, modules) and edges represent relationships (calls, imports, inherits). The platform is split into eight microservices; this gateway is the single entry point for all of them.

The gateway has two roles:

1. **Reverse proxy** -- Forwards requests to backend services (analysis, testing, AI, containers, GitHub, security, realtime) based on URL prefix matching. Handles HTTP and WebSocket traffic.
2. **Domain owner** -- Directly serves endpoints for code graph CRUD, code validation/consistency checking, cache management, Tree-sitter grammar management, dependency visualization, graph metrics/analytics, health monitoring, and Kubernetes job management. These features use local SQLite graph databases and a PostgreSQL database.

## Architecture

```
Client
  |
  v
coding-gateway (:8687)
  |
  |-- Gateway-owned routers (graphs, validation, cache, grammars, visualization, metrics, jobs, health)
  |-- Reverse proxy (catch-all) -->  coding-analysis   (:8688)
  |                              -->  coding-testing    (:8689)
  |                              -->  coding-ai         (:8690)
  |                              -->  coding-containers  (:8691)
  |                              -->  coding-github     (:8692)
  |                              -->  coding-security   (:8693)
  |                              -->  coding-realtime   (:8694)
  |
  |-- PostgreSQL (via PgBouncer) -- ORM tables for nodes, edges, schema_version, audit
  |-- SQLite graph databases     -- Per-container code graphs in /data/graphs/
  |-- Redis                      -- Application cache
```

### Key Components

| Component | Location | Purpose |
|-----------|----------|---------|
| `app_factory.py` | `src/` | Creates the FastAPI app, wires middleware and routers |
| `openapi_aggregator.py` | `src/` | Fetches and merges OpenAPI specs from all backends into a single spec |
| `config/settings.py` | `src/config/` | Pydantic-settings configuration (env vars) |
| `db/session.py` | `src/db/` | Async SQLAlchemy engine + session factory (PostgreSQL) |
| `db/graph_database.py` | `src/db/` | Async SQLite graph database connection (aiosqlite) |
| `models/database.py` | `src/models/` | SQLAlchemy ORM models (Node, Edge, SchemaVersion, NodeAudit) |
| `models/schemas.py` | `src/models/` | Pydantic domain models (entities, relations, validation, cache) |
| `auth/dependencies.py` | `src/auth/` | Re-exports auth dependencies from `coding-client` |
| `middleware/` | `src/middleware/` | Auth, security, validation, metrics middlewares |
| `services/code_graph/` | `src/services/` | Consistency checking, reference validation, graph population, query interface |
| `services/` | `src/services/` | Cache manager, grammar manager, visualization, job manager, metrics |

### Middleware Stack (request processing order)

1. SecurityMiddleware -- Security headers (when `ENABLE_AUTH=true`)
2. ValidationMiddleware -- Request validation
3. AuthMiddleware -- Extracts user identity from Istio-forwarded headers or license token, sets `request.state.user`
4. GZipMiddleware -- Response compression (>1KB)
5. CORSMiddleware -- Cross-origin handling (configured in FastAPI, not Caddy/Traefik)
6. PrometheusMiddleware -- Metrics collection (when `starlette_exporter` is installed)
7. Process time header (`X-Process-Time`)

### Authentication

All auth logic lives in the `coding-client` shared library.

**AuthMiddleware** (`coding_client.auth.middleware`) sets `request.state.user` using this priority:

1. **Dev/test API key** -- In `development`/`testing` environments, `Authorization: Bearer <DEV_API_KEY|TEST_API_KEY>` grants admin access.
2. **Istio-forwarded headers** -- `x-user-id`, `x-tenant-id`, `x-user-roles`, `x-user-name`, `x-user-email`, `x-organization-id`. These are injected by Istio after ext_authz validation.
3. **Trusted-network bypass** -- Requests from RFC1918/loopback IPs (configurable via `TRUSTED_CIDRS`) are allowed with `DEFAULT_TENANT_ID`.
4. **Reject** -- Untrusted network with no headers returns 401.

**`require_tenant`** dependency (`coding_client.auth.tenant`) extracts tenant ID with this priority:

1. `X-Tenant-Id` header (set by Istio ext_authz or gateway proxy)
2. `Authorization: Bearer <token>` -- license token introspection via `licenses.licenses.svc.cluster.local:8544/license/introspect`
3. `request.state.user.effective_tenant_id` (set by AuthMiddleware)
4. Trusted-network bypass with `DEFAULT_TENANT_ID`

Public paths exempt from auth: `/`, `/health`, `/ready`, `/metrics`, `/openapi.json`, `/docs`, `/redoc`.

## API Routes

### System

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Service info (name, version, docs URL) |
| GET | `/health` | Root health check (uptime, component status) |
| GET | `/metrics` | Prometheus metrics (when starlette_exporter available) |
| GET | `/docs` | Swagger UI |
| GET | `/redoc` | ReDoc |
| GET | `/openapi.json` | Aggregated OpenAPI spec (gateway + all backends) |

### Health (`/api/health`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Comprehensive health check (pings all 7 backends) |
| GET | `/api/health/ready` | Kubernetes readiness probe |
| GET | `/api/health/live` | Kubernetes liveness probe |
| GET | `/api/health/health/ready` | Readiness alias (monolith compat) |
| GET | `/api/health/health/live` | Liveness alias (monolith compat) |
| GET | `/api/health/detailed` | Detailed health with system resources, backend reachability |
| GET | `/api/health/system-metrics` | CPU, memory, disk metrics (psutil) |
| GET | `/api/health/metrics` | Full system metrics (psutil: system, network, process) |
| GET | `/api/health/containers/unhealthy` | List unhealthy containers (proxied to coding-containers) |
| GET | `/api/health/container/{name}` | Container-specific health (proxied) |
| GET | `/api/health/container/{name}/history` | Container health history (proxied) |
| GET | `/api/health/container/{name}/ready` | Container readiness probe (proxied, returns 503 if not ready) |
| GET | `/api/health/container/{name}/live` | Container liveness probe (proxied) |
| GET | `/api/health/container/{name}/statistics` | Container statistics (proxied) |
| POST | `/api/health/containers/batch` | Batch container health check (proxied) |

### Health Metrics (`/api/health/metrics`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health/metrics` | System metrics (psutil) |
| GET | `/api/health/metrics/entity/{entity_name}/metrics` | Entity centrality metrics |
| GET | `/api/health/metrics/graph-summary` | Graph summary (entity/relationship counts, density) |
| POST | `/api/health/metrics/centrality` | Calculate and store centrality metrics |
| GET | `/api/health/metrics/entity-type/{entity_type}/metrics` | Centrality metrics by entity type |
| GET | `/api/health/metrics/top-entities` | Top entities by metric (query: `metric`, `limit`) |
| GET | `/api/health/metrics/metrics-comparison` | Compare entity types by metric |
| GET | `/api/health/metrics/dashboard-summary` | Analytics dashboard summary |
| GET | `/api/health/metrics/entity-rankings` | Entity rankings across multiple metrics |
| GET | `/api/health/metrics/multi-language/performance` | Multi-language performance metrics |
| GET | `/api/health/metrics/multi-language/languages/{language}/stats` | Per-language performance stats |
| GET | `/api/health/metrics/multi-language/parser-performance` | Parser performance comparison |
| GET | `/api/health/metrics/multi-language/top-languages` | Top languages by performance |
| POST | `/api/health/metrics/multi-language/export` | Export metrics |
| POST | `/api/health/metrics/multi-language/reset` | Reset metrics |
| GET | `/api/health/metrics/container/{container_name}/entity/{entity_name}/metrics` | Container-scoped entity metrics |
| GET | `/api/health/metrics/container/{container_name}/overview` | Container graph overview |
| GET | `/api/health/metrics/container/{container_name}/health` | Container health metrics |
| POST | `/api/health/metrics/container/{container_name}/refresh` | Refresh container metrics |
| GET | `/api/health/metrics/containers/comparison` | Compare metrics across containers |

### Code Graphs (`/api/graphs`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/graphs` | List all graphs (with optional stats) |
| POST | `/api/graphs/initialize` | Initialize a new code graph for a container |
| GET | `/api/graphs/{container_name}` | Get graph details (node/edge counts) |
| DELETE | `/api/graphs/{container_name}` | Delete a graph and its SQLite database |
| GET | `/api/graphs/{container_name}/nodes` | Query nodes (filter by type, name pattern; paginated) |
| GET | `/api/graphs/{container_name}/edges` | Query edges (filter by type; paginated) |
| GET | `/api/graphs/{container_name}/ai-augmented` | AI-augmented graph analysis |
| GET | `/api/graphs/{container_name}/runtime-augmented` | Runtime-augmented graph analysis |
| GET | `/api/graphs/{container_name}/augmented` | Architecture-augmented graph (quality, dependencies, runtime flags) |

### Validation (`/api/validation`)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/validation/file-reference` | Check if a file exists in a codebase |
| POST | `/api/validation/function-reference` | Check if a function/method exists |
| POST | `/api/validation/class-reference` | Check if a class exists |
| POST | `/api/validation/code-snippet` | Validate code against real codebase entities |
| POST | `/api/validation/function-consistency` | Check if function description matches its code |
| POST | `/api/validation/class-consistency` | Check if class description matches its code |
| POST | `/api/validation/module-consistency` | Validate consistency across modules |
| POST | `/api/validation/code` | Generic code validation (syntax, semantic, style, security) |
| POST | `/api/validation/architecture` | Architecture rule validation |
| POST | `/api/validation/status` | Update validation status (called by GitHub Actions) |
| POST | `/api/validation/complete` | Mark validation complete (called by GitHub Actions) |
| GET | `/api/validation/{validation_id}` | Get validation status and results |

### Cache (`/api/cache`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/cache/stats` | Cache statistics (hits, misses, memory) |
| DELETE | `/api/cache/clear` | Clear all cache entries |
| DELETE | `/api/cache/clear/{container_name}` | Clear cache for a specific container |
| GET | `/api/cache/keys` | List cache keys (optional pattern filter) |
| GET | `/api/cache/key/{cache_key}` | Get a specific cache entry |
| DELETE | `/api/cache/key/{cache_key}` | Delete a specific cache entry |
| POST | `/api/cache/preload` | Preload cache entries |
| GET | `/api/cache/eviction-history` | Cache eviction history |
| GET | `/api/cache/export` | Export cache data (JSON or CSV) |
| POST | `/api/cache/import` | Import cache data |
| GET | `/api/cache/container/{container_name}/stats` | Per-container cache stats |
| POST | `/api/cache/container/{container_name}/warm` | Warm cache for a container |
| POST | `/api/cache/container/{container_name}/invalidate-pattern` | Invalidate keys by pattern |
| POST | `/api/cache/container/{container_name}/analysis` | Cache analysis (cold keys, memory distribution) |

### Grammars (`/api/grammars`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/grammars/status` | Grammar installation and version status |
| POST | `/api/grammars/install` | Install grammars for specific languages |
| POST | `/api/grammars/install-all` | Install all supported grammars |
| POST | `/api/grammars/install-all-async` | Start async grammar installation |
| GET | `/api/grammars/tasks/{task_id}` | Check async task status |
| POST | `/api/grammars/check-updates` | Check for grammar updates |
| DELETE | `/api/grammars/cleanup` | Remove old grammar versions |
| GET | `/api/grammars/supported-languages` | List supported languages |

### Visualization (`/api/visualization`)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/visualization/create-graph` | Create cross-language dependency visualization |
| GET | `/api/visualization/capabilities` | Get supported visualization types, formats, layouts |
| GET | `/api/visualization/examples` | Example configurations and best practices |

### Jobs (`/api/jobs`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/jobs` | List active Kubernetes jobs |
| POST | `/api/jobs` | Create a new pytest Kubernetes job |
| GET | `/api/jobs/health` | Job service health check |
| GET | `/api/jobs/{job_name}` | Get job status, logs, metadata |
| DELETE | `/api/jobs/{job_name}` | Delete a job |

### Proxied Routes

All other paths matching these prefixes are forwarded to the corresponding backend:

| Prefix | Backend |
|--------|---------|
| `/api/analysis`, `/api/bug-hunter`, `/api/quality` | coding-analysis (:8688) |
| `/api/tests` | coding-testing (:8689) |
| `/api/ai` | coding-ai (:8690) |
| `/api/workspaces` | coding-containers (:8691), canonical repository workspace API |
| `/api/containers` | coding-containers (:8691), legacy compatibility alias |
| `/api/validation/multi-line-code-snippet` | coding-containers (:8691) |
| `/api/repositories`, `/api/webhooks/pr-actions` | coding-github (:8692) |
| `/api/security`, `/api/dependencies` | coding-security (:8693) |
| `/api/ws-health`, `/api/realtime`, `/api/broadcast` | coding-realtime (:8694) |

WebSocket proxy endpoints:
- `ws /api/realtime/ws/{path}` -- forwarded to coding-realtime
- `ws /api/broadcast/subscribe` -- forwarded to coding-realtime

## Configuration

All settings are sourced from environment variables (or `.env` file) via `src/config/settings.py`.

| Variable | Default | Description |
|----------|---------|-------------|
| `SERVICE_NAME` | `Code Graph Helper API` | Display name |
| `VERSION` | `1.0.0` | API version |
| `ENVIRONMENT` | `production` | `development`, `staging`, `production`, `test`, `testing` |
| `DEBUG` | `false` | Enable debug logging and SQLAlchemy echo |
| `PORT` | `8687` | Listening port |
| `ENABLE_AUTH` | `true` | Enable security middleware |
| `ENABLE_DOCS` | `false` | Enable Swagger/ReDoc |
| `DEFAULT_TENANT_ID` | `n8n-dev` | Fallback tenant for trusted-network requests |
| `SECRET_KEY` | `change-me-in-production` | Session secret (must change in non-dev environments) |
| `DATABASE_URL` | `postgresql+psycopg://...pgbouncer:6432/codegraph` | PostgreSQL connection string |
| `DB_CONNECTION_TIMEOUT` | `30` | Connection timeout (seconds) |
| `DB_STATEMENT_TIMEOUT` | `30000` | Statement timeout (milliseconds) |
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection string |
| `ANALYSIS_URL` | `http://coding-analysis:8688` | Backend service URL |
| `TESTING_URL` | `http://coding-testing:8689` | Backend service URL |
| `AI_URL` | `http://coding-ai:8690` | Backend service URL |
| `CONTAINERS_URL` | `http://coding-containers:8691` | Backend service URL |
| `GITHUB_URL` | `http://coding-github:8692` | Backend service URL |
| `SECURITY_URL` | `http://coding-security:8693` | Backend service URL |
| `REALTIME_URL` | `http://coding-realtime:8694` | Backend service URL |
| `SHARED_GRAPHS_PATH` | `/data/graphs` | Directory for SQLite graph databases |
| `TEMP_DIR` | `/tmp/code-graph-helper` | Temporary file directory |
| `CA_CERT_PATH` | `/etc/ssl/certs/ca-certificates.crt` | CA certificate path |
| `OTEL_ENDPOINT` | `http://otel-collector:4318` | OpenTelemetry collector |
| `LOG_LEVEL` | `INFO` | Logging level |
| `LOG_FORMAT` | `json` | Log format (`json` or `text`) |
| `CORS_ORIGINS` | `*` | Comma-separated allowed origins |
| `RATE_LIMIT_REQUESTS` | `100` | Requests per window |
| `RATE_LIMIT_WINDOW` | `60` | Rate limit window (seconds) |
| `MAX_CONCURRENT_JOBS` | `3` | Max simultaneous K8s jobs |
| `JOB_TTL_SECONDS` | `1800` | K8s job TTL after completion (seconds) |
| `JOB_BACKOFF_LIMIT` | `2` | K8s job retry limit |
| `JOB_TIMEOUT_SECONDS` | `3600` | K8s job timeout |
| `PYTEST_IMAGE` | `192.168.1.114:5000/pytest-runner:latest` | Docker image for pytest jobs |
| `ENABLE_CACHING` | `true` | Feature flag (reserved) |
| `ENABLE_BACKGROUND_PROCESSING` | `true` | Feature flag (reserved) |
| `ENABLE_FULL_TEXT_SEARCH` | `true` | Feature flag (reserved) |

Additional env vars consumed by `coding-client` auth (not in `settings.py`):

| Variable | Default | Description |
|----------|---------|-------------|
| `KUBERNETES_NAMESPACE` | `code-graph-helper` | K8s namespace for job management |
| `TRUSTED_CIDRS` | `127.0.0.1/32,::1/128,192.168.0.0/16,172.16.0.0/12,10.0.0.0/8` | Trusted network CIDRs for auth bypass |
| `DEV_API_KEY` | (empty) | API key for dev mode auth |
| `TEST_API_KEY` | (empty) | API key for test mode auth |
## Dependencies

### Runtime
- **FastAPI** + **uvicorn** -- ASGI server
- **httpx** -- Reverse proxy HTTP client
- **websockets** -- WebSocket proxy
- **SQLAlchemy** (async) + **psycopg** -- PostgreSQL ORM
- **aiosqlite** -- Async SQLite for graph databases
- **networkx** -- Graph analysis and centrality calculations
- **redis** -- Application caching
- **pydantic-settings** -- Configuration management
- **kubernetes** -- K8s job management
- **coding-client** -- Shared auth, models, and response helpers
- **aiohttp** -- Grammar downloads
- **astor** -- Python AST utilities
- **alembic** -- Database migrations (run via K8s initContainer)
- **OpenTelemetry** -- Distributed tracing
- **structlog** -- Structured logging

### External Services
- **PostgreSQL** (via PgBouncer) -- ORM data (nodes, edges, audit)
- **Redis** -- Application cache
- **7 backend microservices** -- Proxied through the gateway
- **Kubernetes API** -- Job creation and monitoring
- **License introspection** -- `licenses.licenses.svc.cluster.local:8544` (tenant resolution from Bearer tokens)

## Development

```bash
# Install dependencies
uv sync --dev

# Run locally
uv run uvicorn src.main:app --host 0.0.0.0 --port 8687 --reload

# Run tests
uv run pytest

# Lint + type check
uv run ruff check src/ tests/
uv run ty check src/
```

Database migrations are managed by Alembic and run via a K8s initContainer before the gateway starts.

## CI/CD

GitHub Actions workflow: `ruff + ty → build amd64 → smoke test → push`

Image pushed to `ghcr.io/nonagenticai/coding-gateway:sha-<commit>` and tagged `:latest` on success. Flux auto-deploys to K3s.
