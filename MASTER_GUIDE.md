# Grok API Orchestrator — Complete Master Guide

> **Everything you need**: every file explained, every terminal command written out,
> the full data flow from HTTP request to Grok and back, how to store your API keys,
> how to run tests, and how to scale to production. Read this top-to-bottom once and
> you will understand the entire system.

---

## Table of Contents

1. [What This System Actually Does (Plain English)](#1-what-this-system-actually-does)
2. [Visual Architecture — How the Layers Connect](#2-visual-architecture)
3. [Complete Project File Tree](#3-complete-project-file-tree)
4. [Layer-by-Layer File Explanations](#4-layer-by-layer-file-explanations)
   - Foundation: `core/` — config, exceptions, logging
   - Database: `db/` — engine, session, base model
   - Data Model: `models/` — the api_keys table
   - API Contracts: `schemas/` — what goes in and comes out
   - Business Logic: `services/` — grok_client, key_manager, orchestrator
   - HTTP Layer: `api/routes/` — thin route handlers
   - Observability: `metrics/` — latency tracking
   - Entry Point: `main.py` — wiring everything together
   - Tests: `tests/` — how every piece is verified
5. [Full Request Lifecycle — Step by Step](#5-full-request-lifecycle)
6. [Terminal Walkthrough — From Zero to Running](#6-terminal-walkthrough)
7. [How to Store and Manage Your Grok API Keys](#7-how-to-store-and-manage-your-grok-api-keys)
8. [All Admin Operations (with curl)](#8-all-admin-operations)
9. [Understanding the Scoring Algorithm](#9-understanding-the-scoring-algorithm)
10. [Failure Scenarios — What Happens When Things Break](#10-failure-scenarios)
11. [Running the Tests](#11-running-the-tests)
12. [Moving to Production](#12-moving-to-production)
13. [Environment Variables Reference](#13-environment-variables-reference)

---

## 1. What This System Actually Does (Plain English)

Imagine you have five Grok API keys. Without this system, your code picks one key
and hammers it until it hits a rate limit, then crashes. With this system, you have
a smart traffic controller sitting between your application and Grok's servers.

Every time your application wants to ask Grok something, the request goes to our
orchestrator first. The orchestrator looks at all your keys, scores each one based
on how healthy it is (how many recent failures, how fast it responds, when it was
last used), picks the best one, and sends the request. If that key gets rate-limited,
it is put in a "cooling down" state and the orchestrator automatically tries the next
best key — all without your application ever knowing a failure happened. If a key
repeatedly fails, it gets permanently disabled until a human operator reviews and
re-enables it.

The system tracks latency, failure counts, and cooldown timers for every key in a
database, so state is preserved across restarts. It exposes an admin API so you can
add, inspect, update, or remove keys without touching the database directly.

Think of it as a "load balancer and health monitor for API keys" — the same concept
used by production systems at companies managing thousands of API credentials.

---

## 2. Visual Architecture

```
YOUR APPLICATION
       │
       │  POST /v1/ask-ai
       │  {"messages": [...]}
       ▼
┌──────────────────────────────────────────────────────────────────┐
│  FASTAPI LAYER  (app/api/routes/ai.py, keys.py)                  │
│                                                                  │
│  • Receives the HTTP request                                     │
│  • Validates JSON shape via Pydantic schemas                     │
│  • Assigns a unique X-Request-ID to every request                │
│  • Calls the Orchestrator and maps its result to HTTP codes      │
│  • Never contains business logic                                 │
└──────────────────────────┬───────────────────────────────────────┘
                           │ calls
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│  ORCHESTRATOR  (app/services/orchestrator.py)                    │
│                                                                  │
│  • The decision engine — the brain of the system                 │
│  • Implements the Observe → Decide → Act → Learn loop            │
│  • Runs the retry loop (max N different keys)                    │
│  • Classifies failures and routes each type to the right handler │
│  • Stateless — all mutable state lives in the DB                 │
└─────────┬────────────────────────────┬───────────────────────────┘
          │ asks "who is best?"        │ sends actual request
          ▼                            ▼
┌─────────────────────┐    ┌───────────────────────────────────────┐
│  KEY MANAGER        │    │  GROK CLIENT                          │
│  (key_manager.py)   │    │  (grok_client.py)                     │
│                     │    │                                       │
│  • Scores all keys  │    │  • Pure HTTP communication layer      │
│  • Filters cooldowns│    │  • Builds the JSON payload for Grok   │
│  • Updates state    │    │  • Maps HTTP 429/401/5xx to typed     │
│  • Runs all DB ops  │    │    exceptions                         │
│    for api_keys     │    │  • Connection pooling via httpx       │
└─────────┬───────────┘    └───────────────────────────────────────┘
          │
          ▼
┌──────────────────────────────────────────────────────────────────┐
│  DATABASE  (SQLite dev / PostgreSQL prod)                        │
│  Table: api_keys                                                 │
│                                                                  │
│  id | alias | status | fail_count | cooldown_until | avg_latency │
│   1 | key-1 | active |          0 |           null |       102.3 │
│   2 | key-2 | r_ltd  |          0 |    2024-01-01T… |       88.1 │
│   3 | key-3 | active |          2 |           null |       455.0 │
└──────────────────────────────────────────────────────────────────┘
          +
┌──────────────────────────────────────────────────────────────────┐
│  METRICS TRACKER  (app/metrics/tracker.py)                       │
│  In-process rolling window: p50/p95/p99 latency, success rates   │
└──────────────────────────────────────────────────────────────────┘
```

---

## 3. Complete Project File Tree

```
grok_orchestrator/                  ← project root
│
├── .env.example                    ← template for your secrets (safe to commit)
├── requirements.txt                ← all Python dependencies with pinned versions
├── README.md                       ← quick-start reference
├── MASTER_GUIDE.md                 ← this file
│
├── app/                            ← the actual application package
│   ├── __init__.py                 ← makes `app` a Python package
│   ├── main.py                     ← FastAPI app factory + lifespan manager
│   │
│   ├── core/                       ← shared infrastructure (no business logic)
│   │   ├── __init__.py
│   │   ├── config.py               ← all settings from environment variables
│   │   ├── exceptions.py           ← typed exception hierarchy
│   │   └── logging_config.py       ← structured JSON logging setup
│   │
│   ├── db/                         ← database engine and session management
│   │   ├── __init__.py
│   │   ├── base.py                 ← SQLAlchemy DeclarativeBase + TimestampMixin
│   │   └── session.py              ← async engine + get_db dependency
│   │
│   ├── models/                     ← SQLAlchemy ORM models (database table shapes)
│   │   ├── __init__.py             ← imports APIKey so Alembic can find it
│   │   └── api_key.py              ← the api_keys table with all fields
│   │
│   ├── schemas/                    ← Pydantic schemas (API request/response shapes)
│   │   ├── __init__.py
│   │   ├── api_key.py              ← create / read / update / stats schemas
│   │   └── request.py              ← AIRequest + AIResponse schemas
│   │
│   ├── services/                   ← all business logic lives here
│   │   ├── __init__.py
│   │   ├── grok_client.py          ← HTTP wrapper for Grok API (no decisions)
│   │   ├── key_manager.py          ← key scoring, state machine, DB operations
│   │   └── orchestrator.py         ← retry loop + failure classification
│   │
│   ├── api/                        ← FastAPI route handlers (HTTP layer only)
│   │   ├── __init__.py
│   │   └── routes/
│   │       ├── __init__.py
│   │       ├── ai.py               ← POST /v1/ask-ai  (public endpoint)
│   │       └── keys.py             ← /admin/keys/*    (protected endpoints)
│   │
│   └── metrics/                    ← observability
│       ├── __init__.py
│       └── tracker.py              ← in-process rolling metrics (p50/p95/p99)
│
└── tests/                          ← test suite (mirrors app structure)
    ├── __init__.py
    ├── conftest.py                 ← shared fixtures (fake DB, mock Grok client)
    ├── test_key_manager.py         ← unit tests for scoring + state transitions
    ├── test_orchestrator.py        ← unit tests for retry + failure routing
    └── test_routes.py              ← integration tests for HTTP routes
```

---

## 4. Layer-by-Layer File Explanations

Each layer is explained in the order data flows through the system — from
the outermost shell (config) inward to the database, then back out to the HTTP layer.

---

### FOUNDATION LAYER — `app/core/`

These three files are the shared utilities that every other part of the system
depends on. They don't talk to Grok, they don't query the database — they just
provide reliable infrastructure: "what are our settings?", "what type of error
is this?", "how do we write a log line?"

---

#### `app/core/config.py`

**What it does:** This file is the single source of truth for every configurable
value in the system — timeouts, retry counts, scoring weights, database URLs,
secret keys. It uses Pydantic's `BaseSettings` which automatically reads values
from environment variables (or a `.env` file), so you never have to hardcode
anything. The `@lru_cache()` decorator ensures the `.env` file is only read once,
at startup.

**Why it matters:** Without a central config file, settings would be scattered
as magic numbers across dozens of files. When you want to change `MAX_RETRIES`
from 3 to 5, you'd have to hunt down every place that number appears. With this
file, you change exactly one line (or one environment variable) and every part
of the system picks it up.

**How it fits into the flow:** `config.py` is imported by almost every other
module. The `KeyManager` reads `COOLDOWN_SECONDS` and `FAILURE_THRESHOLD`. The
`Orchestrator` reads `MAX_RETRIES`. The `GrokClient` reads `GROK_REQUEST_TIMEOUT`.
They all go through `settings = get_settings()` — one shared, cached object.

```python
# How other files use it:
from app.core.config import settings

if fail_count >= settings.FAILURE_THRESHOLD:  # reads from .env or env var
    disable_key()
```

---

#### `app/core/exceptions.py`

**What it does:** Defines a hierarchy of typed exception classes. Every failure
mode in the system has its own exception class. This is the "vocabulary of failures"
that lets different layers of the system communicate precisely about what went wrong.

**Why it matters:** This is perhaps the most architecturally important file in
the project. Consider the alternative: if `GrokClient` just raises a plain `Exception`
with the text "Request failed", the `Orchestrator` would have to parse strings to
figure out whether to cooldown the key or disable it permanently. Typed exceptions
make failure handling deterministic and easy to test.

The hierarchy is designed so you can catch at any level of specificity:

```
OrchestratorError              ← catch this to handle ANY application error
├── NoAvailableKeyError        ← catch this for "pool is empty" situations
├── AllRetriesExhaustedError   ← catch this for "tried everything, gave up"
└── GrokAPIError               ← catch this for ANY Grok API error
    ├── RateLimitError         ← 429 → cooldown and retry
    ├── AuthenticationError    ← 401/403 → disable immediately
    ├── GrokTimeoutError       ← timeout → increment fail_count and retry
    ├── GrokServerError        ← 5xx → increment fail_count and retry
    └── GrokClientError        ← other 4xx → do NOT retry (our fault)
```

**How it fits into the flow:** `GrokClient` raises these exceptions after
receiving an HTTP response. The `Orchestrator` catches them with specific
`except` blocks and calls the right `KeyManager` method for each type.
The FastAPI routes catch the highest-level ones (`NoAvailableKeyError`,
`AllRetriesExhaustedError`) and convert them to HTTP status codes.

---

#### `app/core/logging_config.py`

**What it does:** Configures `structlog`, a library that produces structured
(key=value or JSON) log output instead of plain text strings. It also provides
two utility functions — `bind_request_context()` which attaches a `request_id`
to every log line within a request, and `get_logger(__name__)` which every module
uses to get its own named logger.

**Why structured logging matters:** In production, your logs go into a system
like CloudWatch, Datadog, or the ELK stack. Plain text logs like `"Key 3 failed"`
are nearly impossible to query efficiently. Structured logs like
`{"level": "warning", "key_id": 3, "alias": "prod-key-1", "event": "key_failure", "request_id": "abc-123"}`
allow you to write queries like "show me all failures for key_id=3 in the last hour."

**The `bind_request_context` pattern:** When a request arrives, the middleware in
`main.py` calls `bind_request_context(request_id="abc-123")`. From that point
forward, every single log call anywhere in the stack — in the Orchestrator, in
the KeyManager, in the GrokClient — automatically includes `"request_id": "abc-123"`
without any code needing to pass it around manually. This is what lets you trace
a single user request through the entire system.

```python
# In any service file, you just do:
log = get_logger(__name__)
log.info("key_selected", key_id=3, latency_ms=102.4)
# Output: {"event": "key_selected", "key_id": 3, "latency_ms": 102.4,
#          "request_id": "abc-123", "logger": "app.services.key_manager", ...}
```

---

### DATABASE LAYER — `app/db/`

These files handle how Python objects get stored in and retrieved from the
database. This layer has no knowledge of Grok, keys, or scoring — it just
provides reliable database access.

---

#### `app/db/base.py`

**What it does:** Provides two things: `Base` (the parent class all SQLAlchemy
models inherit from) and `TimestampMixin` (a reusable set of `created_at` /
`updated_at` columns).

**Why a shared `Base` matters:** SQLAlchemy's metadata system needs to know about
all your models to create tables and generate migrations. By having all models
inherit from the same `Base`, you can call `Base.metadata.create_all(engine)` once
at startup and every table gets created automatically — you never forget to
register a model.

**The `TimestampMixin` pattern:** Rather than defining `created_at` and `updated_at`
on every model (copy-paste, easy to forget), you inherit this mixin. The
`server_default=func.now()` means the database itself sets the timestamp, not
Python — this is important because it avoids clock-skew issues when multiple
application servers are running.

---

#### `app/db/session.py`

**What it does:** Creates the async SQLAlchemy engine (the database connection
pool) and provides a `get_db()` async generator function that FastAPI uses as a
dependency to give each request its own isolated database session.

**Why async matters here:** FastAPI runs on an async event loop. If you use a
synchronous database driver (like the plain `psycopg2` driver for PostgreSQL),
every database query blocks the entire server — no other request can be handled
while one is waiting for a database response. The async engine (`asyncpg` for
PostgreSQL, `aiosqlite` for SQLite) releases the event loop during I/O, so other
requests can be processed in the meantime.

**The `get_db` pattern explained:**

```python
async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        try:
            yield session        # ← route handler runs HERE with the session
            await session.commit()   # ← if handler returns normally, commit
        except Exception:
            await session.rollback() # ← if handler raises, undo everything
        finally:
            await session.close()    # ← always clean up, no matter what
```

This guarantees that: (a) every successful request has its DB changes saved,
(b) any exception automatically rolls back partial changes, and (c) the
connection is always returned to the pool. The route handler never has to think
about any of this.

---

### DATA MODEL LAYER — `app/models/`

---

#### `app/models/api_key.py`

**What it does:** Defines the `APIKey` SQLAlchemy model — the Python representation
of the `api_keys` database table. Every field in the database corresponds to a
typed attribute on this class.

**The state machine (`status` field):** The `status` field is a string enum
that acts as a state machine with three states:

```
           (first registered)
                 │
                 ▼
            ┌─────────┐
            │ ACTIVE  │ ◄─────────────── record_success()
            └────┬────┘
                 │
    429 received │               Too many failures
                 ▼               (fail_count ≥ threshold)
        ┌──────────────┐                 │
        │ RATE_LIMITED │                 ▼
        └──────┬───────┘          ┌──────────┐
               │                  │ DISABLED │
    cooldown   │                  └──────────┘
    expires    │                   (manual re-enable
               │                    via PATCH /admin/keys/{id})
               └────────────────────────────────────────────►
                                  ACTIVE
```

**Why `fail_count` is consecutive, not total:** A key that fails once and then
succeeds ten times is a healthy key having an occasional bad moment. A key that
fails five times in a row with no successes in between is a key with a serious
problem. By resetting `fail_count` to zero on every success, we measure the
*current health streak*, not lifetime failures. Lifetime failures are tracked
separately in `total_failures` for auditing.

**The `masked_key` property:**

```python
@property
def masked_key(self) -> str:
    return f"{self.api_key[:7]}...{self.api_key[-4:]}"
    # "xai-abc...xyz1"
```

The raw API key is stored in the database but is NEVER returned to any client
through any API endpoint. The Pydantic schema uses `masked_key` instead. This
means even if your admin API is accidentally exposed, attackers cannot harvest
your actual keys from it.

**The composite index:**

```python
Index("ix_api_keys_status_enabled_fail", "status", "is_enabled", "fail_count")
```

Every time the Orchestrator needs to pick a key, it runs a query like "give me
all active, enabled keys ordered by fail_count". Without this index, the database
scans every row. With this index, it reads a pre-sorted structure directly. For
a table with even a few thousand keys, this is the difference between a 1ms query
and a 100ms query.

---

#### `app/models/__init__.py`

**What it does:** Imports `APIKey` and `KeyStatus` from `api_key.py`.

**Why this matters:** Alembic (the migration tool) discovers models by importing
`app.models` and looking at `Base.metadata`. If models aren't imported somewhere
that Alembic can see them, it won't know about them and won't generate migration
scripts for them. The `__init__.py` import ensures the model is always
"registered" with the Base whenever the models package is imported.

---

### API CONTRACT LAYER — `app/schemas/`

Schemas (Pydantic models) define the *shape* of data crossing the API boundary.
They are completely separate from SQLAlchemy models because the database shape
and the API shape are often different — and should be designed independently.

---

#### `app/schemas/api_key.py`

**What it does:** Defines three schemas for the key management API:
`APIKeyCreate` (what you POST to register a key), `APIKeyUpdate` (what you PATCH
to change settings), and `APIKeyRead` (what you GET back — the safe view with
`masked_key` instead of the real key).

**Why the separation between Create and Read:** When you create a key, you send
the actual `api_key` string. But the system should never echo it back. The
`APIKeyRead` schema deliberately omits `api_key` and includes `masked_key` instead.
By using different schemas for input and output, this is enforced at the type level
— there's no code path that could accidentally leak the raw key value.

**The `model_config = ConfigDict(from_attributes=True)` setting:** This tells
Pydantic that it can populate a schema by reading attributes off a SQLAlchemy ORM
object. Without this, you'd have to manually convert `APIKey` objects to
dictionaries. With it, you can just write `APIKeyRead.model_validate(key_orm_object)`
and Pydantic does all the work.

---

#### `app/schemas/request.py`

**What it does:** Defines `AIRequest` (what clients send to `/v1/ask-ai`) and
`AIResponse` (what they get back). Also defines `Message` (a single
user/assistant/system message) and `UsageStats` (token counts).

**The `AIResponse` enrichment:** Notice that `AIResponse` includes fields that
don't come from Grok directly — `key_alias`, `attempts`, and `latency_ms`. These
are added by the Orchestrator after the Grok call completes. This "response
enrichment" pattern is extremely valuable in production because it lets the calling
application understand exactly how its request was served: "It took 2 attempts and
280ms, using key prod-key-2." This data is gold for debugging latency spikes or
identifying which key is causing problems.

---

### BUSINESS LOGIC LAYER — `app/services/`

This is the core of the entire system. The three files here contain all the
intelligence. They have no knowledge of HTTP, no knowledge of FastAPI — they are
pure Python business logic that could be invoked from a CLI, a test, a Celery
task, or an HTTP handler with equal ease.

---

#### `app/services/grok_client.py`

**What it does:** Makes HTTP calls to Grok's API. That is its only job. It
contains zero decision-making logic — it does not decide which key to use, it
does not retry, it does not update any state. It receives a key and a request,
sends the HTTP call, and either returns a `GrokResponse` or raises a typed
exception.

**The `httpx.AsyncClient` singleton:** The client is created once at module import
time and reused across all requests:

```python
grok_client = GrokClient()  # module-level singleton
```

This matters because each `httpx.AsyncClient` maintains an internal connection
pool — a set of pre-established TCP+TLS connections to Grok's servers. Reusing
the client means we reuse these connections (HTTP keep-alive). Creating a new
client per request would mean a new TCP handshake and TLS negotiation for every
single API call, adding 50–200ms of overhead each time.

**The `_raise_for_status` method — why it exists:** Python's `requests` and
`httpx` both have a `raise_for_status()` method that raises a generic
`HTTPStatusError` for any non-2xx response. We don't use it because the
`Orchestrator` needs to know *specifically* whether the failure was a 429, a 401,
or a 500. Generic errors would force the Orchestrator to inspect the exception's
message string (fragile) to figure this out. By mapping status codes to our own
exception types here, the Orchestrator gets a clean, typed signal.

**Timeouts are multi-dimensional:**

```python
httpx.Timeout(
    connect=5.0,    # how long to wait to establish the TCP connection
    read=30.0,      # how long to wait for the response body to arrive
    write=5.0,      # how long to wait to finish sending the request
    pool=2.0,       # how long to wait for an available connection from the pool
)
```

A single `timeout=30` would mean "30 seconds for the whole thing", which is
problematic — what if the connection itself hangs for 28 seconds? We'd waste 28
seconds before finding out the server is unreachable. Splitting timeouts lets us
fail fast on connection issues while being patient with slow responses.

---

#### `app/services/key_manager.py`

**What it does:** This is the "state machine manager" for the key fleet. It owns
all database operations for the `api_keys` table and implements the scoring
algorithm that ranks keys. It is the only place in the codebase that reads from
or writes to the `api_keys` table.

**The scoring algorithm in detail:**

The `_score_keys` method scores every eligible key on three dimensions. The
critical step is *normalisation* — converting each dimension to the 0–1 range
relative to the current candidate pool:

```python
def normalise(values):
    min_v, max_v = min(values), max(values)
    if max_v == min_v:
        return [0.0] * len(values)  # all equal? no preference
    return [(v - min_v) / (max_v - min_v) for v in values]
```

Then the weighted score:

```python
score = (
    0.4 * normalise(fail_count)    # reliability is 40% of the decision
  + 0.4 * normalise(avg_latency)   # speed is 40% of the decision
  + 0.2 * recency_penalty          # fairness is 20% of the decision
  - priority_bonus                 # a "priority=90" key gets a 0.45 reduction
)
# Lower score = more desirable
```

Concrete example with three keys:

```
Key A: fail_count=0, latency=100ms, last_used=10 min ago
Key B: fail_count=1, latency=80ms,  last_used=1 min ago
Key C: fail_count=0, latency=120ms, last_used=never

After normalisation:
  fail:    A=0.0, B=1.0, C=0.0
  latency: A=0.5, B=0.0, C=1.0
  recency: A=0.5, B=1.0, C=0.0  (C never used = best for fairness)

Scores:
  A = 0.4*0.0 + 0.4*0.5 + 0.2*0.5 = 0.30
  B = 0.4*1.0 + 0.4*0.0 + 0.2*1.0 = 0.60
  C = 0.4*0.0 + 0.4*1.0 + 0.2*0.0 = 0.40

Winner: Key A (score 0.30)
```

Key C wins over B despite having similar failures to A because C has never
been used (fairness). Key A wins overall because it combines zero failures,
decent latency, and reasonable recency.

**The EWMA latency update:**

```python
alpha = 0.2
new_avg = alpha * new_sample + (1 - alpha) * old_avg
```

If Key A currently averages 100ms and a new request takes 500ms:
`new_avg = 0.2 * 500 + 0.8 * 100 = 100 + 80 = 180ms`

One outlier moved the average from 100ms to 180ms — significant but not
catastrophic. If the next five requests also take 500ms, the average converges
toward 500ms, accurately reflecting the key's current performance. This
mathematical property — weighting recent samples more without overreacting to
single outliers — is why EWMA is used for latency tracking in production systems.

**The `asyncio.Lock` on state updates:**

```python
async with _state_lock:
    await self.db.execute(update(APIKey)...)
```

Without this lock, two concurrent requests that both discover Key A is being
rate-limited could both attempt to write `status=rate_limited` simultaneously,
potentially causing a race condition where one write overwrites the other's
cooldown timestamp. The lock serializes these writes. Note: this lock is
process-level — for multi-process deployments, you'd use PostgreSQL advisory
locks or `SELECT FOR UPDATE`.

---

#### `app/services/orchestrator.py`

**What it does:** Implements the retry loop and failure classification logic.
It calls `KeyManager` to get a key, calls `GrokClient` to use it, and based
on what happens, either returns a response or tries again with a different key.

**The `tried_key_ids` set:** This is the mechanism that ensures we never try
the same key twice in one request's retry cycle:

```python
tried_key_ids: set[int] = set()

while attempt < max_retries:
    key = await self._select_key(exclude_ids=tried_key_ids)
    tried_key_ids.add(key.id)
    # ... try this key ...
```

If Key 1 fails, it's added to `tried_key_ids`. The next call to `_select_key`
will keep calling `get_best_available_key()` until it gets a key not in
`tried_key_ids`. This works because `record_rate_limit()` has already changed
Key 1's status, so it won't be returned as "best available" anyway — but
`tried_key_ids` is a safety net for cases where status changes haven't propagated
yet.

**Why `GrokClientError` is not retried:**

```python
except GrokClientError as exc:
    # Bad request — our payload is wrong.
    # A different key would receive the same payload and also fail.
    # Do NOT retry. Surface to the caller immediately.
    raise
```

If you send a malformed JSON body to Grok, you'll get a 400 Bad Request. Sending
the same malformed body to three different keys will give you three 400 responses.
Retrying in this case wastes time and API quota. This is a fundamental distinction:
transient errors (timeout, rate limit, server error) are worth retrying because
the *same* request *might* succeed next time. Client errors are not worth retrying
because the *same* request will *definitely* fail again.

**The `_build_response` static method:** After a successful Grok call, the
Orchestrator enriches the response with metadata the caller never had to ask for:

```python
return AIResponse(
    content=grok.content,
    key_alias=key.alias,    # which key served this (alias, not the real key!)
    attempts=attempt,       # how many tries were needed
    latency_ms=total_latency,  # wall-clock time including all retries
)
```

---

### HTTP LAYER — `app/api/routes/`

These files are intentionally thin. They are the translation layer between
"HTTP world" (status codes, headers, JSON bodies) and "Python world" (function
calls, typed objects, exceptions). All business logic has already been delegated
to the services layer.

---

#### `app/api/routes/ai.py`

**What it does:** Exposes the single public endpoint `POST /v1/ask-ai`. It
validates the incoming JSON (Pydantic does this automatically based on the
`AIRequest` schema), creates an `Orchestrator` instance with the request's
database session, calls `handle_request()`, and translates the result or any
exception into an appropriate HTTP response.

**The exception-to-HTTP mapping:**

```
NoAvailableKeyError      →  503 Service Unavailable
AllRetriesExhaustedError →  502 Bad Gateway
GrokClientError          →  400 Bad Request
Any unexpected Exception →  500 Internal Server Error
```

These mappings follow standard HTTP semantics: 503 means "the service itself
is not available right now" (no keys = can't serve anything), 502 means "we
tried to reach an upstream service but it kept failing" (all keys exhausted).

**The `X-Request-ID` pattern:** The middleware in `main.py` generates a UUID
for every request and echoes it in the response header. This is a standard
production pattern: when a user reports a bug and gives you their `X-Request-ID`,
you can find every single log line for their request instantly — even across
thousands of other concurrent requests in the logs.

---

#### `app/api/routes/keys.py`

**What it does:** Exposes the admin API for managing keys. All endpoints require
the `X-Admin-Key` header, validated by the `verify_admin_key` dependency.

**The `verify_admin_key` dependency:**

```python
async def verify_admin_key(
    x_admin_key: str = Header(..., description="Admin API key"),
) -> None:
    if x_admin_key != settings.ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid admin API key")
```

The `Header(...)` with three dots means "required". If the header is missing,
FastAPI returns a 422 automatically before this code even runs. If the header
is present but wrong, we return 401. The `dependencies=[Depends(verify_admin_key)]`
in each route decorator means FastAPI runs this check before the route handler.

**Soft-delete pattern:** The DELETE endpoint doesn't remove the database row —
it sets `is_enabled=False` and `status=disabled`. This preserves audit history
("we had this key, it was active from date X to date Y, total lifetime requests:
N"). If you ever need to permanently delete a key, you'd do it directly in the
database with `DELETE FROM api_keys WHERE id = ?`.

---

### OBSERVABILITY LAYER — `app/metrics/`

---

#### `app/metrics/tracker.py`

**What it does:** Maintains an in-process rolling window of recent requests and
computes latency percentiles (p50, p95, p99) and per-key success rates. It is
a module-level singleton that any part of the application can import and record
to.

**Why percentiles matter more than averages:** Imagine 99 requests take 100ms
and 1 takes 10,000ms. The average is 199ms — this makes your system look fine.
But p99 (the latency at which 99% of requests complete) is 10,000ms — one
in a hundred users is waiting 10 seconds. In production, p95 and p99 latencies
are the metrics that actually reveal user-impacting problems that averages hide.

**The `deque(maxlen=ROLLING_WINDOW_SIZE)` pattern:** A `deque` with a fixed
`maxlen` automatically evicts old entries when new ones are added. This keeps
memory bounded regardless of how long the server runs — you always have the
most recent 1000 requests, never more, never less.

**Production upgrade path:** In a real production system, you'd replace this
with the `prometheus_fastapi_instrumentator` library. Each instance of your
app exposes a `/metrics` endpoint in Prometheus format. Prometheus scrapes all
instances and aggregates the data. Grafana then visualizes it. The `MetricsTracker`
in this codebase is designed to have the same conceptual interface, making that
migration a one-file swap.

---

### ENTRY POINT — `app/main.py`

**What it does:** Creates the FastAPI application instance, registers all
middleware and routes, and manages the application lifecycle (startup and
shutdown logic).

**The `lifespan` context manager:**

```python
@asynccontextmanager
async def lifespan(app):
    # STARTUP — runs once when the server starts
    setup_logging()
    await create_db_tables()
    
    yield  # ← the server is running HERE, handling requests
    
    # SHUTDOWN — runs once when the server is stopping
    await grok_client.close()  # drain the HTTP connection pool gracefully
```

This replaces the older `@app.on_event("startup")` and `@app.on_event("shutdown")`
decorators. The `yield` pattern guarantees that shutdown code runs even if the
server is killed with Ctrl+C.

**The `create_app()` factory pattern:** Rather than having a module-level
`app = FastAPI()`, we wrap it in a function. This makes testing much cleaner —
tests call `create_app()` to get a fresh instance, override dependencies (like
the database session), and run tests without affecting a shared global state.

**Middleware execution order:** FastAPI applies middleware in Last-In-First-Out
order. The `request_id_middleware` is added last, so it executes first. This
means the `request_id` is available in the logging context for every subsequent
middleware and route handler.

---

### TESTS — `tests/`

The tests are structured to mirror the services they test. Each test file is
completely independent and uses a fresh in-memory SQLite database, so tests can
run in any order and in parallel.

---

#### `tests/conftest.py`

**What it does:** Provides shared fixtures that all test files use. A "fixture"
in pytest is a reusable piece of test infrastructure — think of it as a setup
function whose result gets injected into tests that declare they need it.

The most important fixtures:

`db_engine` — Creates a fresh in-memory SQLite database for each test. "In-memory"
means no file is written to disk. The database is created before the test, and
disappears after. This makes tests hermetically isolated.

`db_session` — Wraps the engine in a session and calls `rollback()` after each
test. Even if a test commits data, the rollback ensures the next test starts clean.

`mock_grok_success` / `mock_grok_rate_limit` / `mock_grok_timeout` — These are
mock `GrokClient` objects with pre-programmed behaviors. They use Python's
`unittest.mock.AsyncMock` to fake the `complete()` coroutine. This means tests
never make real network calls to Grok — they're fast, free, and deterministic.

```python
# conftest.py creates this fixture:
@pytest.fixture
def mock_grok_success():
    client = MagicMock(spec=GrokClient)
    client.complete = AsyncMock(return_value=make_mock_grok_response())
    return client

# A test uses it like this:
async def test_happy_path(db_session, mock_grok_success):
    orchestrator = Orchestrator(db=db_session, client=mock_grok_success)
    response = await orchestrator.handle_request(...)
    assert response.content == "Hello from Grok!"
```

---

#### `tests/test_key_manager.py`

**What it tests:** The scoring algorithm, state transitions, and CRUD operations
of `KeyManager`. These are unit tests — they test one class in isolation without
any HTTP layer involved.

Key tests to understand:

`test_selects_lowest_fail_count` — Inserts two active keys (one with fail_count=0,
one with fail_count=1) and verifies the orchestrator picks the healthier one.

`test_recovers_rate_limited_key_after_cooldown` — Inserts a `RATE_LIMITED` key
with a `cooldown_until` timestamp in the past. Verifies that calling
`get_best_available_key()` returns this key AND promotes its status back to `ACTIVE`.

`test_record_failure_disables_at_threshold` — Inserts a key at `FAILURE_THRESHOLD - 1`
failures, calls `record_failure()` once more, and verifies the key becomes `DISABLED`.

`test_record_success_updates_ewma_latency` — Verifies the EWMA formula:
`0.2 * new + 0.8 * old`. Given old=500ms and new=100ms, the result should be
`0.2*100 + 0.8*500 = 420ms`.

---

#### `tests/test_orchestrator.py`

**What it tests:** The retry loop and failure classification. These tests are
unit tests that inject mock `GrokClient` objects to simulate specific failure
scenarios.

`test_rate_limited_key_falls_back` — Programs the mock to raise `RateLimitError`
on the first call and return success on the second. Verifies that the response
has `attempts=2` and that `record_rate_limit()` was called on the first key.

`test_client_error_not_retried` — Programs the mock to always raise
`GrokClientError`. Verifies that `complete()` was called exactly once (not
retried) and the exception propagates up.

`test_all_retries_exhausted_raises` — Programs the mock to always raise
`GrokTimeoutError`. Verifies that after `max_retries` attempts, an
`AllRetriesExhaustedError` is raised with the correct `attempts` count.

---

#### `tests/test_routes.py`

**What it tests:** The HTTP layer — correct status codes, response shapes, header
presence, and authentication. These are integration tests that spin up the full
FastAPI app using `httpx.AsyncClient`.

The key technique is patching the Orchestrator at the route layer:

```python
with patch(
    "app.api.routes.ai.Orchestrator.handle_request",
    AsyncMock(side_effect=NoAvailableKeyError("Pool empty")),
):
    response = await test_client.post("/v1/ask-ai", json={...})

assert response.status_code == 503
```

This tests that the route correctly maps `NoAvailableKeyError` to HTTP 503,
without needing a real database or real keys.

---

## 5. Full Request Lifecycle

Here is the complete journey of a single request, traced through every file:

```
Step 1: Client sends POST /v1/ask-ai
        Body: {"messages": [{"role": "user", "content": "Hello"}]}

Step 2: main.py middleware
        ├── request_id_middleware generates UUID: "req-abc-123"
        ├── bind_request_context("req-abc-123") — all logs will carry this
        └── log_requests middleware starts timing

Step 3: app/api/routes/ai.py — ask_ai()
        ├── Pydantic validates the JSON against AIRequest schema
        ├── If invalid shape → 422 Unprocessable Entity (automatic, no code needed)
        └── Creates Orchestrator(db=session)

Step 4: app/services/orchestrator.py — handle_request()
        ├── attempt = 1, tried_key_ids = set()
        └── Calls _select_key(exclude_ids=set())

Step 5: app/services/key_manager.py — get_best_available_key()
        ├── _fetch_eligible_keys(): SQL query
        │   SELECT * FROM api_keys
        │   WHERE is_enabled = TRUE
        │   AND (status = 'active' OR (status = 'rate_limited' AND cooldown_until <= now()))
        │   ORDER BY fail_count ASC, avg_latency_ms ASC
        ├── Any rate_limited keys with expired cooldown get promoted to active
        ├── _score_keys(): normalise + weight all dimensions
        └── Returns Key with lowest score (e.g., Key #2, alias="prod-key-1")

Step 6: Back in orchestrator.py
        ├── tried_key_ids = {2}
        └── Calls grok_client.complete(api_key=key.api_key, key_id=2, request=...)

Step 7: app/services/grok_client.py — complete()
        ├── Builds payload: {"model": "grok-3", "messages": [...], "temperature": 0.7}
        ├── POST https://api.x.ai/v1/chat/completions
        │   Authorization: Bearer xai-actual-key-value
        ├── Response received: HTTP 200, latency = 234ms
        └── Returns GrokResponse(content="Hello!", latency_ms=234)

Step 8: Back in orchestrator.py (SUCCESS PATH)
        ├── Calls key_manager.record_success(key, latency_ms=234)
        └── Returns _build_response(grok_resp, key, attempts=1, total_latency=235ms)

Step 9: app/services/key_manager.py — record_success()
        ├── new_latency = 0.2 * 234 + 0.8 * old_avg (EWMA)
        └── UPDATE api_keys SET fail_count=0, status='active',
                avg_latency_ms=new_latency, last_used=NOW(),
                total_requests=total_requests+1
            WHERE id=2

Step 10: Back in app/api/routes/ai.py — ask_ai()
         ├── Receives AIResponse object
         ├── Records metrics: metrics_tracker.record_request(...)
         └── Returns HTTP 200 with JSON body:
             {
               "content": "Hello!",
               "model": "grok-3",
               "key_alias": "prod-key-1",   ← alias, never the real key
               "attempts": 1,
               "latency_ms": 235.4,
               "usage": {"prompt_tokens": 8, "completion_tokens": 12, "total_tokens": 20}
             }

Step 11: main.py middleware (cleanup)
         ├── Response header X-Request-ID: "req-abc-123" added
         ├── log_requests logs: {"event": "http_request", "status_code": 200, ...}
         └── clear_request_context() removes request_id from logging context
```

---

## 6. Terminal Walkthrough

Follow these steps exactly, in order, to go from zero to a running server.

### Step 1 — Clone / create the project directory

```bash
# If you have the zip, extract it. Otherwise create the directory:
mkdir grok_orchestrator
cd grok_orchestrator
```

### Step 2 — Create a Python virtual environment

A virtual environment keeps this project's dependencies isolated from your
system Python and other projects. This is non-negotiable for production work.

```bash
# Create the virtual environment (Python 3.11+ recommended)
python3 -m venv venv

# Activate it (you must do this every time you open a new terminal)
source venv/bin/activate          # macOS / Linux
# OR
venv\Scripts\activate             # Windows PowerShell
```

You'll know it's activated when your prompt shows `(venv)` at the beginning.

### Step 3 — Install dependencies

```bash
pip install -r requirements.txt
```

This installs FastAPI, SQLAlchemy, httpx, structlog, pydantic, and all their
sub-dependencies. It will take 30–60 seconds the first time.

### Step 4 — Create your `.env` file

```bash
# Copy the template
cp .env.example .env

# Now edit it with your actual values:
nano .env           # or: code .env  / vim .env / open it in your editor
```

The minimum you MUST change before running:

```env
# Change these two to strong random strings:
API_SECRET_KEY=replace-this-with-a-long-random-string-at-least-32-chars
ADMIN_API_KEY=replace-this-with-another-long-random-string-at-least-32-chars

# Leave DATABASE_URL as SQLite for now (development):
DATABASE_URL=sqlite+aiosqlite:///./grok_orchestrator.db

# These are the Grok API settings — leave as defaults unless you know better:
GROK_BASE_URL=https://api.x.ai/v1
GROK_DEFAULT_MODEL=grok-3
GROK_REQUEST_TIMEOUT=30.0

# These control retry and cooldown behaviour:
MAX_RETRIES=3
COOLDOWN_SECONDS=60
FAILURE_THRESHOLD=5
```

To generate a strong random string for your keys:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
# Example output: a3f8b2c1d9e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0
```

Run this twice and use each output for `API_SECRET_KEY` and `ADMIN_API_KEY`.

### Step 5 — Start the development server

```bash
uvicorn app.main:app --reload --port 8000
```

Breaking this command down:
- `uvicorn` — the ASGI server that runs FastAPI
- `app.main:app` — "look in the `app/main.py` file, find the object named `app`"
- `--reload` — automatically restart when you save any Python file (dev only!)
- `--port 8000` — listen on port 8000

You should see output like:

```
INFO     Waiting for application startup.
INFO     database_tables_ready
INFO     app_ready debug=True
INFO     Application startup complete.
INFO     Uvicorn running on http://127.0.0.1:8000
```

The `database_tables_ready` line tells you SQLite has been initialized and
the `api_keys` table has been created at `./grok_orchestrator.db`.

### Step 6 — Verify the server is running

```bash
curl http://localhost:8000/health
# Expected: {"status":"ok","version":"1.0.0"}
```

### Step 7 — Open the interactive API docs (development mode only)

Visit http://localhost:8000/docs in your browser. You'll see Swagger UI with
every endpoint, their schemas, and a "Try it out" button for each one.
This is automatically generated from your Pydantic schemas — no extra work needed.

---

## 7. How to Store and Manage Your Grok API Keys

Your actual Grok API key values are stored in the `api_keys` table in the
database. You add them at runtime via the admin API — they are never hardcoded
in any file and never in your `.env` file (the `.env` file only holds the
application's own secret keys, not third-party API keys).

### Getting your Grok API key

Go to https://console.x.ai → API Keys → Create API Key. Copy the value —
it will look like `xai-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`.

### Storing your first key

```bash
curl -X POST http://localhost:8000/admin/keys \
  -H "Content-Type: application/json" \
  -H "X-Admin-Key: your-admin-api-key-from-env-file" \
  -d '{
    "api_key": "xai-your-actual-grok-key-here",
    "alias": "primary-key",
    "priority": 50,
    "notes": "Main account key, created 2024-01"
  }'
```

The system responds with the key's database record. Notice the response
contains `masked_key` (like `xai-abc...xyz1`) rather than the real key value —
this is intentional and cannot be changed. The raw key is only stored in the
database, never returned through any endpoint.

### Storing multiple keys

Repeat the same command for each key, choosing different aliases:

```bash
# Key 2 — a backup account
curl -X POST http://localhost:8000/admin/keys \
  -H "Content-Type: application/json" \
  -H "X-Admin-Key: your-admin-api-key-from-env-file" \
  -d '{"api_key": "xai-second-key", "alias": "backup-key", "priority": 30}'

# Key 3 — a premium plan key you want to prefer
curl -X POST http://localhost:8000/admin/keys \
  -H "Content-Type: application/json" \
  -H "X-Admin-Key: your-admin-api-key-from-env-file" \
  -d '{"api_key": "xai-premium-key", "alias": "enterprise-key", "priority": 90}'
```

### Priority explained

Priority values range from 0 to 100. A priority-90 key receives a significant
scoring bonus — it will be preferred over lower-priority keys even if its latency
is slightly higher. Use this for premium-plan keys that have higher rate limits
or lower costs.

---

## 8. All Admin Operations

### List all keys and their current state

```bash
curl http://localhost:8000/admin/keys \
  -H "X-Admin-Key: your-admin-api-key"
```

The response shows every key's `status`, `fail_count`, `avg_latency_ms`,
`last_used`, and `cooldown_until`. This is your fleet health dashboard.

### Check fleet statistics

```bash
curl http://localhost:8000/admin/keys/stats \
  -H "X-Admin-Key: your-admin-api-key"

# Response:
# {
#   "total_keys": 3,
#   "active_keys": 2,
#   "rate_limited_keys": 1,
#   "disabled_keys": 0,
#   "total_requests_lifetime": 1847,
#   "total_failures_lifetime": 23,
#   "avg_latency_ms_fleet": 187.4
# }
```

### Re-enable a disabled key

If a key was automatically disabled (exceeded failure threshold), you can
re-enable it after investigating and confirming the key is still valid:

```bash
curl -X PATCH http://localhost:8000/admin/keys/1 \
  -H "Content-Type: application/json" \
  -H "X-Admin-Key: your-admin-api-key" \
  -d '{"status": "active", "is_enabled": true}'
```

### Manually take a key out of rotation (for maintenance)

```bash
curl -X PATCH http://localhost:8000/admin/keys/2 \
  -H "Content-Type: application/json" \
  -H "X-Admin-Key: your-admin-api-key" \
  -d '{"is_enabled": false}'
```

### Remove a key permanently

```bash
curl -X DELETE http://localhost:8000/admin/keys/3 \
  -H "X-Admin-Key: your-admin-api-key"
# Returns 204 No Content on success
```

### View in-process performance metrics

```bash
curl http://localhost:8000/admin/metrics \
  -H "X-Admin-Key: your-admin-api-key"

# Response includes:
# - uptime_seconds
# - total requests and failures since startup
# - rolling_window: last 1000 requests with avg/p50/p95/p99 latency
# - per_key breakdown: each key's request count and success rate
```

### Make an actual AI request

```bash
curl -X POST http://localhost:8000/v1/ask-ai \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [
      {"role": "system", "content": "You are a helpful assistant."},
      {"role": "user", "content": "What is the capital of France?"}
    ],
    "temperature": 0.7,
    "max_tokens": 256
  }'

# Response:
# {
#   "content": "The capital of France is Paris.",
#   "model": "grok-3",
#   "usage": {"prompt_tokens": 18, "completion_tokens": 9, "total_tokens": 27},
#   "key_alias": "primary-key",
#   "attempts": 1,
#   "latency_ms": 312.7,
#   "finish_reason": "stop"
# }
```

### Force fewer retries for a latency-sensitive request

```bash
curl -X POST http://localhost:8000/v1/ask-ai \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "Quick question: 2+2?"}],
    "max_retries": 1
  }'
```

---

## 9. Understanding the Scoring Algorithm

Here is how to tune the scoring weights in your `.env` file to match your priorities:

```env
# Current defaults:
SCORE_WEIGHT_FAIL_COUNT=0.4   # 40% — reliability
SCORE_WEIGHT_LATENCY=0.4      # 40% — speed
SCORE_WEIGHT_LAST_USED=0.2    # 20% — fairness (spread load evenly)
```

**If you have many keys and want perfectly even distribution** (cost optimization):
```env
SCORE_WEIGHT_FAIL_COUNT=0.3
SCORE_WEIGHT_LATENCY=0.1
SCORE_WEIGHT_LAST_USED=0.6    # heavily favor least-recently-used
```

**If you have latency-sensitive users and some keys are geographically closer:**
```env
SCORE_WEIGHT_FAIL_COUNT=0.2
SCORE_WEIGHT_LATENCY=0.7      # heavily favor fastest key
SCORE_WEIGHT_LAST_USED=0.1
```

**If reliability is everything (mission-critical):**
```env
SCORE_WEIGHT_FAIL_COUNT=0.7   # heavily penalize any key with recent failures
SCORE_WEIGHT_LATENCY=0.2
SCORE_WEIGHT_LAST_USED=0.1
```

---

## 10. Failure Scenarios

### Scenario A: Key hits rate limit (HTTP 429)

```
GrokClient raises RateLimitError
  → Orchestrator catches it
  → key_manager.record_rate_limit(key):
      UPDATE api_keys SET status='rate_limited',
             cooldown_until = NOW() + 60 seconds
      WHERE id = failing_key_id
  → Orchestrator continues loop with attempt+1
  → KeyManager._fetch_eligible_keys() now EXCLUDES this key (status filter)
  → Next best key is selected and tried
  → If that succeeds → 200 response, attempts=2 in metadata
  → Meanwhile, after 60 seconds, cooldown expires; key comes back on next fetch
```

### Scenario B: Key credentials are invalid (HTTP 401)

```
GrokClient raises AuthenticationError
  → Orchestrator catches it
  → key_manager.record_auth_failure(key):
      UPDATE api_keys SET status='disabled', is_enabled=FALSE
      WHERE id = bad_key_id
  → This key will NEVER be automatically re-enabled
  → Next key is tried
  → Operator must investigate and either re-enable or delete the key via PATCH
```

### Scenario C: Grok server is down (HTTP 503 from Grok)

```
GrokClient raises GrokServerError
  → Orchestrator catches it
  → key_manager.record_failure(key):
      UPDATE api_keys SET fail_count = fail_count + 1
      WHERE id = key_id
  → If fail_count < FAILURE_THRESHOLD: status stays 'active', try next key
  → If fail_count >= FAILURE_THRESHOLD: status becomes 'disabled'
  → Since ALL keys talk to the same Grok server, all will fail
  → AllRetriesExhaustedError raised after MAX_RETRIES attempts
  → Client receives HTTP 502 Bad Gateway
```

### Scenario D: All keys exhausted simultaneously

```
All keys are either rate_limited or disabled
  → _fetch_eligible_keys() returns empty list
  → NoAvailableKeyError raised
  → Orchestrator does NOT enter retry loop (this is not retryable)
  → Client receives HTTP 503 Service Unavailable
  → Alert on this: it means your key fleet is undersized or all keys are degraded
```

---

## 11. Running the Tests

```bash
# Make sure you're in the project root with venv activated
cd grok_orchestrator
source venv/bin/activate

# Run all tests
pytest tests/ -v

# Run a specific test file
pytest tests/test_key_manager.py -v

# Run a specific test
pytest tests/test_orchestrator.py::TestRetryBehaviour::test_rate_limited_key_falls_back -v

# Run tests and show coverage
pip install pytest-cov
pytest tests/ --cov=app --cov-report=term-missing
```

Expected output:

```
tests/test_key_manager.py::TestKeySelection::test_selects_lowest_fail_count PASSED
tests/test_key_manager.py::TestKeySelection::test_raises_when_no_keys PASSED
tests/test_key_manager.py::TestKeySelection::test_skips_disabled_keys PASSED
...
tests/test_orchestrator.py::TestRetryBehaviour::test_rate_limited_key_falls_back PASSED
...
tests/test_routes.py::TestAIRoute::test_successful_ai_request PASSED
...
========================= 30 passed in 2.14s =========================
```

All tests use in-memory SQLite and mock HTTP clients — no Grok API key required,
no internet connection needed.

---

## 12. Moving to Production

### Switch to PostgreSQL

```bash
# Install asyncpg (already in requirements.txt)
pip install asyncpg

# Update .env:
DATABASE_URL=postgresql+asyncpg://username:password@your-db-host:5432/grok_orchestrator

# Create the database in PostgreSQL first:
createdb grok_orchestrator
# OR in psql: CREATE DATABASE grok_orchestrator;
```

### Use Alembic for database migrations

With SQLite and development, `create_all()` at startup is fine. In production,
you need migration scripts that can evolve the schema without destroying data:

```bash
# One-time setup
alembic init alembic

# Edit alembic/env.py — add these two lines near the top:
from app.db.base import Base
from app.models import api_key  # ensures model is registered
target_metadata = Base.metadata

# Generate a migration from your current models:
alembic revision --autogenerate -m "initial schema"

# Apply migrations:
alembic upgrade head

# When you add a new column to api_key.py, run:
alembic revision --autogenerate -m "add new_column"
alembic upgrade head
```

### Run with multiple workers (production)

```bash
# gunicorn manages multiple uvicorn worker processes
pip install gunicorn

# 4 workers × (2 × CPU cores + 1) is a common starting formula
gunicorn app.main:app \
  --workers 4 \
  --worker-class uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:8000 \
  --access-logfile - \
  --error-logfile -
```

### Disable debug docs in production

In `.env`, set `DEBUG=false`. The `/docs` and `/redoc` endpoints are automatically
disabled. The health check at `/health` remains available (used by load balancers).

---

## 13. Environment Variables Reference

| Variable | Default | Description |
|---|---|---|
| `DEBUG` | `false` | Enables coloured dev logging, Swagger UI, verbose SQL |
| `LOG_LEVEL` | `INFO` | Minimum log level: DEBUG, INFO, WARNING, ERROR |
| `DATABASE_URL` | SQLite | Full async connection string |
| `GROK_BASE_URL` | `https://api.x.ai/v1` | Grok API base (change for a proxy) |
| `GROK_DEFAULT_MODEL` | `grok-3` | Model used when client doesn't specify one |
| `GROK_REQUEST_TIMEOUT` | `30.0` | Seconds before treating a call as timed out |
| `MAX_RETRIES` | `3` | How many different keys to try per request |
| `COOLDOWN_SECONDS` | `60` | How long a rate-limited key waits before re-entry |
| `FAILURE_THRESHOLD` | `5` | Consecutive failures before a key is auto-disabled |
| `LATENCY_EWMA_ALPHA` | `0.2` | EWMA smoothing factor (higher = more reactive) |
| `SCORE_WEIGHT_FAIL_COUNT` | `0.4` | Reliability weight in key scoring |
| `SCORE_WEIGHT_LATENCY` | `0.4` | Speed weight in key scoring |
| `SCORE_WEIGHT_LAST_USED` | `0.2` | Fairness weight in key scoring |
| `API_SECRET_KEY` | *change me* | Application secret (JWT signing, etc.) |
| `ADMIN_API_KEY` | *change me* | Password for all `/admin/*` endpoints |
