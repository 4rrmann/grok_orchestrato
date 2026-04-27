# Groq API Orchestrator

A production-grade FastAPI backend that acts as an intelligent orchestration layer
for managing multiple Grok API keys. Provides smart load balancing, automatic
failover, per-key health tracking, and structured observability — without
violating any provider terms of service.

---

## Architecture Overview

```
Client Request
      │
      ▼
┌─────────────────────────────────────────────────────┐
│  FastAPI Layer  (app/api/routes/)                   │
│  • HTTP parsing & validation (Pydantic)             │
│  • Request ID injection (middleware)                │
│  • Exception → HTTP status code mapping             │
│  • Zero business logic                              │
└──────────────────────┬──────────────────────────────┘
                       │ delegates to
                       ▼
┌─────────────────────────────────────────────────────┐
│  Orchestrator  (app/services/orchestrator.py)       │
│  • Observe → Decide → Act → Learn loop              │
│  • Retry policy (max N different keys)              │
│  • Failure classification routing                   │
│  • Response enrichment                              │
└──────────┬──────────────────────┬───────────────────┘
           │ asks for best key    │ makes API call
           ▼                      ▼
┌──────────────────┐   ┌─────────────────────────────┐
│  Key Manager     │   │  Grok Client                │
│  • Key selection │   │  • httpx async HTTP         │
│  • Score ranking │   │  • Payload construction     │
│  • State updates │   │  • Status → exception map   │
│  • CRUD ops      │   │  • Connection pooling       │
└──────────┬───────┘   └─────────────────────────────┘
           │ reads/writes
           ▼
┌─────────────────────────────────────────────────────┐
│  Database  (SQLAlchemy async + PostgreSQL/SQLite)   │
│  • api_keys table with full operational state       │
│  • Composite indexes for fast key selection         │
└─────────────────────────────────────────────────────┘
           +
┌─────────────────────────────────────────────────────┐
│  Metrics Tracker  (app/metrics/tracker.py)          │
│  • In-process rolling window                        │
│  • p50/p95/p99 latency percentiles                  │
│  • Per-key success rates                            │
└─────────────────────────────────────────────────────┘
```

---

## Database Schema

```sql
CREATE TABLE api_keys (
    id              INTEGER     PRIMARY KEY AUTOINCREMENT,
    api_key         TEXT        NOT NULL UNIQUE,       -- the actual secret; never logged
    alias           TEXT        NOT NULL DEFAULT 'unnamed',
    status          TEXT        NOT NULL DEFAULT 'active',  -- active | rate_limited | disabled
    is_enabled      BOOLEAN     NOT NULL DEFAULT TRUE,     -- manual on/off switch
    fail_count      INTEGER     NOT NULL DEFAULT 0,         -- consecutive failures; resets on success
    total_requests  INTEGER     NOT NULL DEFAULT 0,         -- lifetime counter
    total_failures  INTEGER     NOT NULL DEFAULT 0,         -- lifetime counter
    last_used       TIMESTAMP,                              -- for fairness scoring
    cooldown_until  TIMESTAMP,                              -- rate-limit expiry
    avg_latency_ms  REAL        NOT NULL DEFAULT 0.0,       -- EWMA latency
    priority        INTEGER     NOT NULL DEFAULT 0,         -- manual tier weight
    notes           TEXT,                                   -- operator freetext
    created_at      TIMESTAMP   NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP   NOT NULL DEFAULT NOW()
);

-- Indexes
CREATE INDEX ix_api_keys_status_enabled_fail ON api_keys(status, is_enabled, fail_count);
```

**Why each field exists:**

| Field | Purpose |
|---|---|
| `status` | State machine node. The orchestrator filters to `active` + expired `rate_limited` keys. |
| `fail_count` | *Consecutive* failures. Resets to 0 on any success. Drives the disability threshold. |
| `avg_latency_ms` | EWMA of response times. Weights recent samples more → adapts quickly to performance changes. |
| `last_used` | Ensures fairness — prevents one key being overloaded while others sit idle. |
| `cooldown_until` | Precise rate-limit recovery time. Key re-enters the pool automatically when this passes. |
| `priority` | Manual override for tiered plans (e.g., an enterprise key you always prefer). |
| `total_requests/failures` | Lifetime audit counters. Never reset. Used for capacity planning and key health reports. |

---

## Request Flow

```
1. POST /v1/ask-ai
   │
2. Middleware: inject X-Request-ID, bind structlog context
   │
3. Route handler: validate request (Pydantic), delegate to Orchestrator
   │
4. Orchestrator: fetch best available key from KeyManager
   │  ┌─ scoring: normalise fail_count + latency + recency → weighted sum
   │  └─ filter: active OR (rate_limited AND cooldown expired)
   │
5. GrokClient: POST to api.x.ai/v1/chat/completions
   │
6a. SUCCESS → KeyManager.record_success(latency_ms)
   │           • fail_count → 0
   │           • EWMA latency update
   │           • last_used = now()
   │           • return enriched AIResponse
   │
6b. HTTP 429 → KeyManager.record_rate_limit()
   │            • status = rate_limited
   │            • cooldown_until = now() + COOLDOWN_SECONDS
   │            • try next key (if retries remain)
   │
6c. HTTP 401/403 → KeyManager.record_auth_failure()
   │                • status = disabled, is_enabled = False
   │                • try next key (if retries remain)
   │
6d. Timeout / 5xx → KeyManager.record_failure()
   │                 • fail_count += 1
   │                 • if fail_count >= FAILURE_THRESHOLD → disable key
   │                 • try next key (if retries remain)
   │
6e. No keys left → NoAvailableKeyError → HTTP 503
6f. All retries exhausted → AllRetriesExhaustedError → HTTP 502
```

---

## Key Selection Algorithm

```python
# For each candidate key, compute:
score = (
    0.4 * normalise(fail_count)    # reliability
  + 0.4 * normalise(avg_latency)   # speed
  + 0.2 * recency_penalty          # fairness
  - priority_bonus                 # tier override
)
# Pick the key with the lowest score
```

**Normalisation** maps each dimension to `[0, 1]` relative to the current
candidate pool so that `fail_count` (range 0–5) and `latency` (range 0–5000ms)
don't clash. A key that has been unused the longest gets `recency_penalty = 0`
(most desirable) — ensuring load is spread fairly.

---

## EWMA Latency

```
new_avg = α × new_sample + (1 - α) × old_avg    (α = 0.2 by default)
```

- A sudden slow response contributes only 20% to the new average
- Persistent slowness degrades the score over time
- A key that recovers its speed will see its score improve within ~10 requests

---

## Quick Start

```bash
# 1. Clone and install
git clone <repo>
cd grok_orchestrator
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env — set ADMIN_API_KEY to something strong

# 3. Run (SQLite, no DB setup needed)
uvicorn app.main:app --reload --port 8000

# 4. Register an API key
curl -X POST http://localhost:8000/admin/keys \
  -H "X-Admin-Key: your-admin-key" \
  -H "Content-Type: application/json" \
  -d '{"api_key": "xai-your-real-key", "alias": "prod-key-1"}'

# 5. Make an AI request
curl -X POST http://localhost:8000/v1/ask-ai \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "Hello, how are you?"}]
  }'
```

---

## API Endpoints

### Public
| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/ask-ai` | Submit an AI completion request |
| `GET` | `/health` | Liveness check (no auth required) |

### Admin (requires `X-Admin-Key` header)
| Method | Path | Description |
|---|---|---|
| `POST` | `/admin/keys` | Register a new API key |
| `GET` | `/admin/keys` | List all keys (paginated) |
| `GET` | `/admin/keys/stats` | Fleet health aggregate |
| `PATCH` | `/admin/keys/{id}` | Update key settings |
| `DELETE` | `/admin/keys/{id}` | Soft-delete a key |
| `GET` | `/admin/metrics` | In-process latency/success metrics |

---

## Production Deployment

### PostgreSQL
```env
DATABASE_URL=postgresql+asyncpg://user:pass@db-host:5432/grok_orchestrator
```

### Alembic migrations (instead of create_all)
```bash
alembic init alembic
# Configure alembic/env.py to use your async engine
alembic revision --autogenerate -m "initial"
alembic upgrade head
```

### Scaling to multiple instances
For multiple app replicas, the in-process `MetricsTracker` won't aggregate
across instances. Replace it with:
- **Prometheus** — each instance exposes `/metrics`, Prometheus scrapes all
- **Redis** — use atomic `INCR`/`HINCRBYFLOAT` for shared counters

For key-state coordination across replicas, the PostgreSQL DB is already
the shared source of truth — no extra work needed. The `asyncio.Lock` in
`KeyManager` becomes a no-op in multi-process mode; use PostgreSQL advisory
locks or `SELECT FOR UPDATE` if you need strict serialisation.

### Recommended additions
- **Redis** for cooldown state — faster than DB queries for high-RPS systems
- **Prometheus + Grafana** — replace `MetricsTracker` with proper counters/histograms
- **Sentry** — add `sentry-sdk[fastapi]` for exception tracking
- **Alembic** — replace `create_all()` with migration-based schema management
- **Docker** — containerise for consistent deployments

---

## Project Structure

```
grok_orchestrator/
├── app/
│   ├── main.py                  # FastAPI app factory + lifespan
│   ├── core/
│   │   ├── config.py            # Pydantic settings (env vars)
│   │   ├── exceptions.py        # typed exception hierarchy
│   │   └── logging_config.py    # structlog setup
│   ├── db/
│   │   ├── base.py              # DeclarativeBase + TimestampMixin
│   │   └── session.py           # async engine + get_db dependency
│   ├── models/
│   │   └── api_key.py           # SQLAlchemy ORM model
│   ├── schemas/
│   │   ├── api_key.py           # Pydantic create/read/update schemas
│   │   └── request.py           # AIRequest + AIResponse schemas
│   ├── services/
│   │   ├── grok_client.py       # httpx wrapper — HTTP only
│   │   ├── key_manager.py       # key state machine + scoring
│   │   └── orchestrator.py      # retry + failure classification loop
│   ├── api/
│   │   └── routes/
│   │       ├── ai.py            # POST /v1/ask-ai
│   │       └── keys.py          # admin key management
│   └── metrics/
│       └── tracker.py           # in-process rolling metrics
├── tests/
│   ├── conftest.py
│   ├── test_key_manager.py
│   ├── test_orchestrator.py
│   └── test_routes.py
├── .env.example
├── requirements.txt
└── README.md
```
