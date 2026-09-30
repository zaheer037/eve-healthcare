# EVE Healthcare backend: architecture

The design of the diagnostic-booking service: what it assumes, how it is built, the data model, the
state machines, the API, the payment and webhook flows, the security model, and the reasons behind
the choices. Priority labels: P0 is required by the assignment, P1 is hardening that the service
ships, and P2 is an optional extension that is not built. [README.md](../README.md) says how to run it, and [perf.md](perf.md) reports what it
does at 100,000 bookings.

## 1. Assumptions

The PDF leaves these points open. The service makes one choice for each, and each choice is listed here.

| # | Assumption |
|---|---|
| A1 | **Currency.** All prices are in INR. We store a `currency` code on each price, booking and payment so a second currency does not need a schema change. |
| A2 | **Roles.** Signup always creates a `PATIENT`. The client can never choose a role. An `ADMIN` comes from the seed script (development default `admin@example.com`, documented in the README). The catalog is public to read and admin-only to write. |
| A3 | **"Patient/user".** The patient is the authenticated user. A user cannot book for another person in v1. |
| A4 | **Capacity and time.** Each centre has unlimited capacity and no opening hours. An appointment must start between 30 minutes and 365 days from now. The start time is truncated to the minute. Both limits are settings. |
| A5 | **Pricing.** The server copies the price at booking time into `bookings.amount`. The client never sends an amount. A later price change does not change old bookings. |
| A6 | **Payment failure.** A `FAILED` payment sets the booking to `FAILED`. This status is final. The user creates a new booking to try again (ADR-09). Without the `simulate` field, `POST /payments/` always ends in `SUCCESS` or `FAILED`. |
| A7 | **Cancellation.** A patient can cancel a `PENDING` or `CONFIRMED` booking. Refunds are out of scope. A cancelled `CONFIRMED` booking keeps its `SUCCESS` payment, and the README lists refunds as a known gap. |
| A8 | **Unpaid bookings.** A `PENDING` booking does not expire in v1. The user can cancel it. A `PENDING` payment exists only when the request sets `simulate=PENDING`, which is allowed outside production. Only a webhook settles it. |
| A9 | **Webhook contract.** The PDF does not define the webhook payload. We define a flat one (§6.5). The simulated provider signs each request with a shared secret. |
| A10 | **Event delivery.** The provider delivers at least once and in any order. The receiver stores every signed event that has a valid body and answers `200`. It answers non-`2xx` only when the request is unusable (`401`, `413`, `422`) or when a retry can fix the error (`503`). |
| A11 | **Tokens.** Access tokens only, 30 minutes. No refresh token and no revocation list in v1. |
| A12 | **Wire format.** JSON with `snake_case` names. Timestamps are ISO 8601 with a UTC offset. Money is a decimal string, for example `"499.00"`. |
| A13 | **Paths.** The canonical form follows the PDF (`/payments/`, `/payments/webhook/`). Every route also answers with the other slash spelling and never redirects, because webhook senders such as Stripe treat a redirect as a failed delivery. |
| A14 | **Deletion.** Rows are never deleted, because bookings reference centres and tests. `DELETE` deactivates a row (`is_active = false`). `PATCH {"is_active": true}` restores it. Inactive rows are hidden from every read. An admin restores a row by its known ID. |
| A15 | **Login identifier.** The email address is the login. Emails are unique without regard to case. We store them in lowercase. The login field is a plain string and not an `EmailStr`, so any stored address can log in. |
| A16 | **Personal data.** We store the email, an optional name and an optional phone number. We store no clinical data. |
| A17 | **Duplicates and volume.** One user cannot hold two active bookings (`PENDING` or `CONFIRMED`) for the same test, centre and minute. One user can hold about 20 active future bookings (a setting). The cap is soft: two simultaneous requests can pass it together (§5.4). |
| A18 | **Payment simulator.** The simulator succeeds with a configurable probability (default 80%). A simulated failure carries the reason `declined_by_simulator`. Outside production, a request field can force `SUCCESS`, `FAILED` or `PENDING`. `PENDING` means "the provider confirms later by webhook". |
| A19 | **Password policy.** 15 to 128 characters and no composition rules. This is the NIST SP 800-63B-4 baseline for a password that is the only factor. Passwords are never stripped and are NFC-normalised. The minimum is a setting, so it can be lowered for a friendlier demo (ADR-13). |
| A20 | **Webhooks settle, never create.** A webhook only settles a payment that already exists. It never creates a payment or a booking. This is why a duplicate cannot create a second payment. |
| A21 | **Payment window.** A booking whose appointment has started cannot be paid. |
| A22 | **Text input.** Text fields reject control characters (Unicode category `Cc`, so also TAB, LF and DEL), bidirectional override characters and lone surrogates (category `Cs`, which have no UTF-8 form). A description also allows TAB and LF. Numeric fields match the database `CHECK` bounds. A `PATCH` with `null` is rejected for every field that is not nullable. The schemas aim to keep client input from reaching the database as an unmapped error. |


## 2. Architecture

### 2.1 System context

```mermaid
flowchart LR
    subgraph clients["Clients"]
        C["Patient / Admin<br/>HTTP client"]
        PP["Simulated payment provider<br/>scripts/simulate_provider.py"]
    end
    subgraph compose["docker compose"]
        API["FastAPI app<br/>fastapi run"]
        MIG["migrate<br/>alembic upgrade head, then seed<br/>(runs once)"]
        DB[("PostgreSQL 17")]
        RD[("Redis<br/>optional, P2")]
    end
    C -- "HTTPS + Bearer JWT" --> API
    PP -- "signed webhook" --> API
    API -- "SQLAlchemy 2.0 + psycopg 3" --> DB
    API -. "catalog cache, shared throttle counters" .-> RD
    MIG --> DB
```

The API keeps no state between requests, except the in-memory login-throttle counters (P1). State lives in PostgreSQL. You can run several API containers behind a load balancer without code changes. Each container then counts throttle hits on its own (§8.4).

### 2.2 Layers and the dependency rule

```mermaid
flowchart TD
    subgraph http["HTTP layer (knows FastAPI)"]
        R["router.py<br/>handlers"]
        D["dependencies.py<br/>auth, service wiring"]
        S["schemas.py<br/>Pydantic input and output"]
    end
    subgraph app["Application layer (knows no HTTP)"]
        SV["service.py<br/>use cases, transaction boundary"]
        ST["state.py, policy.py, settlement.py<br/>pure domain rules"]
    end
    subgraph data["Persistence layer"]
        RP["repository.py<br/>SQL queries"]
        M["models/<br/>tables and constraints"]
    end
    DB[("PostgreSQL")]
    CORE["core/<br/>settings, security, errors, logging, db, pagination, clock, routing, body limit, throttle"]
    R --> D --> SV
    R --> S
    SV --> ST
    SV --> RP --> M --> DB
    R -.-> CORE
    SV -.-> CORE
    RP -.-> CORE
```

**Import rules.** Ruff enforces the framework bans (`TID251` with per-file ignores). `fastapi` stays in routers, dependencies and `core`. SQLAlchemy sessions stay in repositories, `core.db` and tests. `import-linter` (P2) enforces the finer layer contract.

| Layer | Can import | Must not import |
|---|---|---|
| `router.py`, `dependencies.py`, `schemas.py` | Own `service`, `core`, `models` enums, other packages' `schemas`, `auth.dependencies` (current user and admin check) | `repository`, SQLAlchemy `Session` queries |
| `service.py` | Own `repository`, `state`/`policy`/`settlement`, `core` and `models`. Other packages' `repository` modules and pure rules | `fastapi`, any router, any other service |
| `repository.py`, `inbox.py` | `models`, `core.pagination`, SQLAlchemy | `service`, `fastapi` |
| `state.py`, `policy.py`, `settlement.py`, `signature.py` | Standard library, `models` (enums and entities), `core.errors`, other packages' pure rules (`settlement.py` calls `bookings.state.transition`) | `fastapi`, SQLAlchemy `Session`, `core.db`, any `repository` |
| `models/` | SQLAlchemy, `models.base` | Everything else |
| `core/` | Standard library and third-party libraries | Any feature package |

**Transaction rule.** A service method is one use case and one transaction. A method that writes or locks ends its transaction itself before it returns: `commit()` on success, `rollback()` on failure. This releases every lock before FastAPI serialises the response. A read-only method takes no lock, and the session dependency closes its transaction. Repositories and pure rules never commit. Services never call `session.begin()`, which raises an error after the session has begun. On an `IntegrityError`, the service calls `rollback()` first and then raises the domain error, so the session stays usable. `PaymentService.pay` is one transaction too (ADR-05). The in-process simulator does no I/O, so no lock spans a network call. §6.6 describes the two-phase flow that a real gateway needs.

**Aggregate rule.** The booking is the aggregate root. Every change to a booking or to one of its payments first locks the booking row. The lock order is booking, then payment. One lock per aggregate means no deadlock between the payment path, the webhook path and cancellation. Every locking read follows four rules:

1. **`FOR NO KEY UPDATE`**, written `with_for_update(key_share=True)`. A child insert takes `FOR KEY SHARE` on its parent row. Plain `FOR UPDATE` conflicts with that lock, and two webhooks for one payment then deadlock.
2. **`populate_existing=True`.** Without it, the ORM returns the stale object that is already in the session.
3. **The owner predicate.** The lock query reads `WHERE id = :id AND user_id = :user`. A miss returns `None`, and the service then makes a plain existence read. `owner_error(exists, not_found)` in `bookings/policy.py` turns that read into `Forbidden` (the row exists) or the `not_found` error. Repositories cannot import `core.errors`, so they raise no domain error. Four call sites share `owner_error`: `cancel`, `pay`, `BookingService.get_owned` and `PaymentService.get_owned`. ADR-07 is one branch in it. A stranger never waits behind the owner's lock.
4. **One table, no `joinedload`.** A locking query selects one table.

**Why no abstract repository.** A `Protocol` with one implementation adds a file and a name and removes no risk. The one boundary that gains a second implementation later is the payment provider. Today the simulator is a concrete class, and tests choose its result.

### 2.3 Repository layout

```text
eve_healthcare/
├── pyproject.toml            # dependencies, Ruff, mypy, pytest, [tool.fastapi] entrypoint
├── uv.lock                   # committed lockfile
├── requirements.txt  requirements-dev.txt   # generated by `make requirements`, for readers without uv
├── README.md  Dockerfile  docker-compose.yml  docker-compose.dev.yml  Makefile
├── .env.example  .gitignore  .dockerignore  .pre-commit-config.yaml
├── alembic.ini
├── .github/                  # CI workflow and Dependabot
├── migrations/
│   ├── env.py
│   └── versions/             # 0001_users.py ... 0004_payments.py
├── app/
│   ├── main.py               # create_app(): lifespan, middleware, handlers, routers
│   ├── core/
│   │   ├── config.py         # Settings (pydantic-settings) and production validators
│   │   ├── db.py             # engine, sessionmaker, get_session, transaction, domain_errors, retry_transient
│   │   ├── security.py       # hash/verify password (NFC), hash_slot dependency, create/decode access token
│   │   ├── throttle.py       # auth throttle: limits library, in-memory (P1)
│   │   ├── errors.py         # AppError hierarchy = the error catalogue (§5.3)
│   │   ├── problem.py        # RFC 9457 model, exception handlers, problems() helper for responses=
│   │   ├── routing.py        # TolerantRouter: both slash spellings and HEAD, no redirect
│   │   ├── body_limit.py     # BodyLimitMiddleware: problem+json 413
│   │   ├── http.py           # request ID, access log and no-store middleware, located(), bind_request_id
│   │   ├── logging.py        # configure_logging() in the lifespan, database-error scrubber
│   │   ├── schemas.py        # SafeText, SafeParagraph, Money, CurrencyCode, PatchModel
│   │   ├── pagination.py     # PageParams, Page[T]
│   │   ├── clock.py          # Clock type, utcnow
│   │   └── health.py         # /health/live, /health/ready
│   ├── models/               # the whole schema in one folder
│   │   ├── base.py           # Base, naming convention, mixins
│   │   └── user.py  catalog.py  booking.py  payment.py
│   ├── auth/                 # router  schemas  service  repository  dependencies
│   ├── catalog/              # router  schemas  service  repository
│   ├── bookings/             # router  schemas  service  repository  state.py  policy.py
│   └── payments/             # router  schemas  service  repository  dependencies
│                             # simulator.py  signature.py  settlement.py  inbox.py
├── scripts/
│   ├── seed.py               # admin and sample data. `--scale N` adds N bookings
│   ├── seed_data.py          # the sample catalog: 12 tests, 5 centres, 30 prices
│   ├── seed_load.py          # the bulk bookings and payments of `--scale`
│   ├── perf_report.py        # `explain` and `latency` at 100,000 bookings
│   ├── simulate_provider.py  # signed webhook sender: duplicates, retries, backoff
│   └── errors.py             # SeedError, the one refusal of the tools
├── tests/
│   ├── conftest.py  factories.py
│   ├── unit/                 # pure functions, no database
│   └── integration/          # HTTP + real PostgreSQL
└── docs/                     # ARCHITECTURE.md (this document) and perf.md
```

The `models/` folder is shared on purpose. The whole schema reads in one place. Alembic imports one module. Repositories can join across tables without importing another feature package. The `scripts/` folder is a package that the image copies (§10.1), so `python -m scripts.seed` works in the container and on a laptop.

**Three small pieces of infrastructure carry a rule each.** Tests run them on Starlette 1.7.0.

```python
# app/core/routing.py — both slash spellings, HEAD next to GET, never a redirect.
class TolerantRouter(APIRouter):
    def add_api_route(self, path, endpoint, **kwargs):
        super().add_api_route(path, endpoint, **kwargs)
        hidden = {**kwargs, "include_in_schema": False}
        has_get = "GET" in (kwargs.get("methods") or ())
        if has_get:
            super().add_api_route(path, endpoint, **{**hidden, "methods": ["HEAD"]})
        alt = path[:-1] if path.endswith("/") else path + "/"
        if alt != path and (alt or self.prefix):  # skip the empty path of a root route
            super().add_api_route(alt, endpoint, **hidden)
            if has_get:
                super().add_api_route(alt, endpoint, **{**hidden, "methods": ["HEAD"]})
```

Every feature router is a `TolerantRouter`, and the app sets `redirect_slashes=False`. `/payments` and `/payments/` both reach the same handler. `HEAD` answers wherever `GET` does. Only the canonical `GET` spelling appears in the OpenAPI document. The first route with a name is the canonical one, so `url_path_for` returns the canonical path.

**`Allow` on `405`.** Starlette lists the methods of the first matching route only. So `PUT /bookings/` answers `Allow: GET`, although the path also serves `HEAD` and `POST` (RFC 9110 §15.5.6 asks for every supported method). The problem handler for `405` builds the header itself. It asks each route whether it fully matches the request path with each served method (`GET`, `HEAD`, `POST`, `PUT`, `PATCH`, `DELETE`), through Starlette's `route.matches`. It cannot read a flat list of routes. FastAPI 0.137 and newer keep an included router behind a private wrapper.

`BodyLimitMiddleware` is a pure ASGI middleware with one cap (64 KiB, a setting). A request with a `Content-Length` above the cap gets a problem+json `413` before the application runs. A chunked body has no length header, so the middleware reads at most the cap into memory and replays it to the application. A chunked body over the cap gets the same `413`, and the application never runs, even on a route that ignores the body. A request that names both a `Content-Length` and a chunked encoding gets a `400` (RFC 9112 section 6.1). The httptools parser refuses it already. The h11 parser passes both headers on, and a small length would hide a chunked body of any size from the cap. The response carries every problem field, including `request_id` and `instance`. Starlette's own middleware answers `text/plain` instead, and for a route without a body it runs the handler before it answers. The largest legitimate body is under 2 KiB.

`NoStoreMiddleware` adds `Cache-Control: no-store` and `Pragma: no-cache` to every response that passes through it: successes, `401`, `404`, `405`, `413` and `422`. The catch-all `500` response does not pass through it, because Starlette sends that response from `ServerErrorMiddleware`, outside every user middleware. So the `500` handler sets `Cache-Control`, `Pragma` and `X-Request-ID` itself. A header that a route or a dependency sets reaches only the `200`.

### 2.4 Request lifecycle

1. Uvicorn accepts the request.
2. Middleware runs in this order, outermost first: request ID, access event (P1), no-store headers, CORS (only when `CORS_ORIGINS` is set), body-size limit. The access event sits just inside the request ID, so it also logs the `413` that the body limit answers. The request ID and the user ID live in `scope["state"]`, where every handler and the catch-all `500` handler can read them.
3. The router matches the path in either slash spelling.
4. FastAPI reads the body and decodes the JSON. Malformed JSON returns `422` here, before authentication runs.
5. Dependencies resolve: database session, current user (decode the JWT, load the user), role check, page parameters, service wiring. A failed authentication stops the request with `401` or `403`.
6. FastAPI validates path, query and body fields. A failure returns `422`. A request with a well-formed body, invalid fields and no token gets `401`, not `422`, because step 5 comes first. Tests for authentication send a valid body.
7. The handler calls one service method.
8. The service reads through repositories, applies pure rules, writes, ends its transaction and logs one business event.
9. The handler returns an ORM object. `response_model` filters and serialises it.
10. An `AppError` becomes a problem response. A validation error becomes a problem response. Starlette's own `404` and `405` become problem responses with `ROUTE_NOT_FOUND` and `METHOD_NOT_ALLOWED`. An unhandled exception becomes a `500` problem response. Its handler sets the request ID in the body and in `X-Request-ID`, and it sets both cache headers (§2.3). The log keeps a scrubbed stack trace (§8.2). The client does not see it.
11. The session dependency closes the session and rolls back any uncommitted work. It declares `scope="function"`. The exit code then runs after the handler returns and the response model is serialised, but before the response is sent. The connection returns to the pool early, even when the client is slow.

### 2.5 Function catalogue

Contracts only. Exact signatures come with the code. Statement counts are asserted by tests (`tests/integration/test_query_budgets.py`).

**`core`**

| Function | Contract | Notes |
|---|---|---|
| `get_settings()` | Returns the cached `Settings`. Stops the start-up when `ENVIRONMENT` is missing. It also stops when a secret is missing, too short or a known placeholder outside `development`. In production it stops when `WEBHOOK_SIGNATURE_REQUIRED` is `false` (§8.3). | Overridden in tests. |
| `get_session()` | Yields one `Session` per request. Closes it on exit. | `scope="function"`. `expire_on_commit=False`. |
| `retry_transient(fn, retry_on_constraints=, attempts=3)` | Runs a whole transaction again after SQLSTATE 40001 or 40P01. It also does so after a unique violation on a constraint that the caller names (the webhook names `uq_payments_provider_ref`). Waits a short random time between attempts. After the last attempt it raises `ServiceUnavailable` (`503`) from the database error. It does not retry a lock timeout (55P03). `is_transient(error, constraints)` holds the rule. | The webhook uses it (§6.3). `core` names no payment constraint. |
| `transaction(session)` | A context manager for one use case: it commits when the block ends and rolls back when the block raises. Every service method that writes or locks runs inside it, so a lock never outlives the call. | `cancel`, `pay` and the webhook use it. |
| `domain_errors(session, by_constraint)` | A context manager that rolls back and then turns a violated constraint into the domain error that names it. A constraint that is not in the mapping is raised unchanged. | Build the errors for each call: an exception carries its traceback. |
| `hash_password(plain)` | NFC-normalises the password. Returns an Argon2id hash. | CPU-bound, 55 to 67 ms with the library defaults on an idle 8-core machine. |
| `verify_password(plain, hash)` | Returns `bool`. Same normalisation. | Constant-time compare. |
| `HashSlots.slot()` | A context manager over a `threading.BoundedSemaphore` (`HASH_MAX_CONCURRENCY`, default 4). It takes a slot with `acquire(blocking=False)`. A full semaphore raises `ServiceUnavailable` at once (`503`, `Retry-After`). It releases the slot on exit. | `auth_gate` uses it on the event loop. A waiting semaphore inside a sync handler starved the pool. |
| `create_access_token(user_id, settings)` | Returns `(token, expires_in)`. Claims: `sub` (a string), `iat`, `exp`, `iss`, `aud`. Uses the real clock, because PyJWT checks `iat` and `exp` against it. | HS256. Builds `jwt.PyJWT(options={"enforce_minimum_key_length": True})`. |
| `decode_access_token(token, settings)` | Returns the user ID (a `UUID`) or raises `InvalidToken`, for every failure alike. Pins the algorithm. Requires `exp`, `sub`, `iss` and `aud`, and checks `iss` and `aud`. | No clock argument, because PyJWT checks against the real clock. |
| `LoginThrottle` | `check(ip, email=None)` counts the request against the IP limit and raises `RateLimited` (with `Retry-After`) when the IP limit or the account limit is spent. `record_failure(email)` counts one failed login. `reset(email)` clears the account counter after a good login. The account key is the SHA-256 of the NFC-normalised, lower-cased email, so case variants share one counter and any email costs a fixed-size key. | `limits` library, in-memory, moving window (§7.2). |
| `PageParams` / `Page[T]` | `limit` 1–100 (default 20), `offset` 0–10,000. `Page` holds `data` and `pagination`. | |
| `scrub_database_errors` | A structlog processor. It walks `__cause__` and `__context__` and replaces a database error with its class, SQLSTATE and constraint name. | PostgreSQL error text echoes row values such as the email. |
| `configure_logging()` | Runs in the lifespan start-up, after Uvicorn has configured its own loggers. Sets structlog with `show_locals=False` and the scrubber. Disables `uvicorn.access`. Drops the traceback from `uvicorn.error` records. | Uvicorn's access log prints the query string, and its traceback prints the row values of a database error. |

**`auth`**

| Function | Contract | Commits | Raises |
|---|---|---|---|
| `AuthService.signup(data)` | Lower-cases the email. Hashes the password. Inserts a `PATIENT`. One statement, no pre-check. | yes | `EmailAlreadyRegistered` |
| `AuthService.login(email, password)` | Loads the user by email. Verifies the password. When the user is missing, verifies against the dummy hash that start-up computed. Records a failure or clears the account counter. Returns an access token. | no | `InvalidCredentials` |
| `auth_gate(request, email=None)` | An async context manager that `signup_gate`, `login_gate` and `token_gate` use. Each gate is an `async def` dependency that returns the parsed input, so the handler does not declare the body twice. `auth_gate` calls `LoginThrottle.check` and then takes a hashing slot, both on the event loop, before the request needs a pool thread. The signup gate first checks `PASSWORD_MIN_LENGTH`, which a schema cannot, and raises the same 422 as a schema error. | no | `RateLimited`, `ServiceUnavailable` |
| `get_current_user(token)` | Dependency. Reads the token with `auto_error=False`. Decodes it. Loads the user by ID. | no | `NotAuthenticated`, `InvalidToken` |
| `require_admin(user)` | Dependency. Passes only for `ADMIN`. | no | `Forbidden` |

**`catalog`**

| Function | Contract | Commits | Raises |
|---|---|---|---|
| `list_centres(filters, page)` | Active centres. Filters: `city`, `test_id`, `q`. When `test_id` is set, each centre carries the price of that test. Two statements (count, page). | no | — |
| `get_centre(id)` | Active centre with its available tests and prices. Two statements. | no | `CentreNotFound` |
| `create_centre` / `update_centre` / `deactivate_centre` | Admin. `update` accepts `is_active`, so `PATCH` restores a row. `deactivate` sets `is_active` to `false`. | yes | `CentreNotFound`, `DuplicateCentre` |
| `list_tests`, `get_test`, `create_test`, `update_test`, `deactivate_test` | Same shape as centres. | yes (writes) | `DiagnosticTestNotFound`, `DuplicateDiagnosticTest` |
| `set_centre_test(centre_id, test_id, price, currency, is_available)` | Admin. One upsert statement that also sets `updated_at`. Returns the offered test and a flag that says whether the row is new. Maps a foreign key violation to `404`. | yes | `CentreNotFound`, `DiagnosticTestNotFound` |

**`bookings`**

| Function | Contract | Commits | Raises |
|---|---|---|---|
| `BookingService.create(user_id, centre_id, test_id, appointment_at)` | Takes plain values, not the schema. Loads the active centre-test with its price. Verifies the appointment window. Counts the user's active future bookings against the cap. Inserts a `PENDING` booking with the price snapshot. Maps the unique violation to `DuplicateBooking`. Three statements. | yes | `CentreTestNotFound`, `AppointmentOutOfWindow`, `BookingLimitReached`, `DuplicateBooking` |
| `list_for_user(user_id, page)` | Own bookings, newest first, with the id as a tie-breaker. Centre and test names come from one join. Two statements. | no | — |
| `get_owned(user_id, booking_id)` | One statement with the owner predicate. The booking, its names and all its payments arrive together. A miss triggers one more read, and `owner_error` picks `403` or `404`. | no | `BookingNotFound`, `Forbidden` |
| `cancel(user_id, booking_id)` | Runs inside `core.db.transaction`. Locks the owned booking (`owner_error` on a miss). `CANCELLED` returns unchanged. Refuses when a live `PENDING` payment exists. Four statements: the lock, the payment check, the `UPDATE` and one read of the centre and test names. A repeat takes two. | yes | `BookingNotFound`, `Forbidden`, `InvalidBookingStatus`, `PaymentInProgress` |
| `state.transition(current, target)` | Pure. Enforces the transition table and returns the target. | — | `InvalidBookingStatus` |
| `policy.check_appointment_window(at, now=, min_lead=, max_advance=)` | Pure. Both ends of the window are allowed. | — | `AppointmentOutOfWindow` |
| `policy.check_active_cap(active=, cap=)` | Pure. | — | `BookingLimitReached` |
| `policy.owner_error(exists=, not_found=)` | Pure. The one place that chooses `403` or `404` (ADR-07). Keyword arguments, so a bare `True` never reads as a mystery. | — | returns `Forbidden` or `not_found` |

**`payments`**

| Function | Contract | Commits | Raises |
|---|---|---|---|
| `PaymentService.pay(user_id, booking_id, simulate)` | One transaction. Locks the owned booking (`owner_error` on a miss). Returns the live payment when one exists (replay). Otherwise checks that the booking is `PENDING` and the appointment is in the future, asks the simulator, builds a `PENDING` payment, runs `settle()` and commits. One flush writes both rows. Four statements on create, two on replay. | yes | `BookingNotFound`, `Forbidden`, `BookingNotPayable`, `SimulationNotAllowed` |
| `PaymentService.get_owned(user_id, payment_id)` | One join statement with the owner predicate. A miss triggers one more read, and `owner_error` picks `403` or `404`. | no | `PaymentNotFound`, `Forbidden` |
| `PaymentService.handle_webhook(payload)` | Records the event. Locks booking, then payment. Checks amount, currency and provider reference. Settles. Marks the outcome. One transaction, retried up to three times after a deadlock, a serialization failure or a provider-reference collision. Eight statements when it applies a result. | yes | none for business cases (they are outcomes) |
| `settlement.settle(booking, payment, result, now)` | Pure. Decision table in §4.3. | — | — |
| `settlement.event_mismatch(stored_amount, stored_currency, amount, currency)` | Pure. Returns `amount_mismatch`, `currency_mismatch` or `None`. The webhook rejects an event that does not describe its payment. | — | — |
| `policy.check_payable(booking_id, status, appointment_at, now)` | Pure, in `bookings/policy.py`. The booking must be `PENDING` and its appointment must lie ahead. | — | `BookingNotPayable` |
| `signature.sign(secret, timestamp, body)` / `header_for(…)` / `verify(secret, header, body, now)` | Pure. Constant-time compare on bytes. Tolerance 300 seconds. One error for a missing, a malformed, a wrong and a stale signature. The provider script uses `header_for`. | — | `WebhookSignatureInvalid` |
| `PaymentSimulator.charge(payment_id, override)` | Returns `PaymentResult` or `None` (`PENDING` mode). Uses the success rate unless `override` is set and allowed. No I/O. The reference is `sim_<payment id>`, and a failure carries the reason `declined_by_simulator`. The dice are injected, so a test chooses. | — | — |
| `WebhookInbox.record(provider, payload)` / `mark(event_id, outcome, detail, payment_id, processed_at)` | `record` runs `INSERT … ON CONFLICT DO NOTHING RETURNING id` with `payment_id` set to `NULL`. It returns the row ID or `None` (duplicate). `mark` sets the outcome, the time and the payment together, as the `CHECK` needs. | no | — |
| `verified_webhook_payload(request)` | Async dependency. Reads the raw bytes. Verifies the signature. Parses the body. Converts a `ValidationError` into `RequestValidationError`. | no | `WebhookSignatureInvalid` |

---

## 3. Data model

### 3.1 Entity-relationship diagram

```mermaid
erDiagram
    users ||--o{ bookings : places
    centres ||--o{ centre_tests : offers
    diagnostic_tests ||--o{ centre_tests : "is offered as"
    centre_tests ||--o{ bookings : "is booked as"
    bookings ||--o{ payments : "is paid by"
    payments |o--o{ webhook_events : "is updated by"

    users {
        uuid id PK
        text email UK
        text password_hash
        text full_name "nullable"
        text phone "nullable"
        varchar role
    }
    centres {
        uuid id PK
        text name
        text location
        text city "nullable"
        boolean is_active
    }
    diagnostic_tests {
        uuid id PK
        text code UK
        text name UK
        text description
        boolean is_active
    }
    centre_tests {
        uuid centre_id PK, FK
        uuid test_id PK, FK
        numeric price
        text currency
        boolean is_available
    }
    bookings {
        uuid id PK
        uuid user_id FK
        uuid centre_id FK
        uuid test_id FK
        timestamptz appointment_at
        numeric amount
        text currency
        varchar status
    }
    payments {
        uuid id PK
        uuid booking_id FK
        numeric amount
        text currency
        varchar status
        text provider
        text provider_payment_id
        text failure_reason
        timestamptz settled_at
    }
    webhook_events {
        uuid id PK
        text provider
        text event_id
        uuid payment_id FK "nullable"
        jsonb payload
        varchar outcome
        varchar detail
        timestamptz received_at
        timestamptz processed_at
    }
```

`bookings (centre_id, test_id)` is one composite foreign key to `centre_tests (centre_id, test_id)`. Every table except `webhook_events` also has `created_at` and `updated_at` (`timestamptz`). The PDF asks for a centre with a name, a location and its tests with prices. `centres` holds `name`, `location` and an optional `city` for filtering, and `centre_tests` holds the price of each test at each centre.

### 3.2 Conventions

| Topic | Rule |
|---|---|
| Names | Tables: plural `snake_case`. One naming convention on `MetaData` builds the names of single-column constraints: `pk_`, `fk_`, `uq_`, `ix_`, `ck_` + table + column. Every multi-column constraint and every index gets an explicit name, because the convention uses the first column only. Migrations wrap explicit names in `op.f()`. Names stay under 63 characters. |
| IDs | UUIDv4 from `uuid.uuid4()`, created in Python, so the ID is known before the flush. `centre_tests` uses its natural composite key. `# ponytail: UUIDv4 fragments B-tree indexes past ~10M rows → UUIDv7 (Python 3.14 stdlib or PostgreSQL 18 uuidv7())`. |
| Time | `timestamptz` only. The session runs in UTC. `created_at` defaults to `now()`. The ORM sets `updated_at` on change (`onupdate=func.now()` with `eager_defaults=True`, so `RETURNING` fills the value and no refresh query runs). An upsert bypasses `onupdate`, so it sets `updated_at=func.now()` itself. The API returns ISO 8601 with `Z`. |
| Money | `numeric(10,2)` with `CHECK (x BETWEEN 0.01 AND 99999999.99)`. A plain `CHECK (x > 0)` accepts `NaN`, because PostgreSQL sorts `NaN` above every number. `Decimal` in Python. String in JSON. Pydantic rejects more than two decimal places, so the database never rounds (it rounds half away from zero). A `currency` column holds an ISO 4217 code. |
| Status columns | `varchar(32)` plus a named `CHECK`, built from a Python `StrEnum` with `sa.Enum(..., native_enum=False, length=32, create_constraint=True, name="status", values_callable=...)`. A bare `Mapped[StrEnum]` annotation silently creates a **native** PostgreSQL enum that stores member names, so we never use it. A test asserts that the schema has no native enum type. Alembic never updates the `CHECK`, so a new status needs a hand-written migration. |
| Booleans | `is_active` and `is_available` replace deletion (assumption A14). |
| Relationships | `lazy="raise_on_sql"` on every relationship. `Booking.centre` and `Booking.test` are `viewonly=True` with an explicit `primaryjoin`, because the composite foreign key targets `centre_tests`. `Booking.payments` is view-only too, ordered by `created_at` and `id`. The write paths never go through a relationship. |
| Text length | Pydantic enforces the limits (for example `full_name` has 1–100 characters). The 64 KiB body cap bounds every value. `# ponytail: no length CHECK → add CHECK (char_length(x) <= n) if another writer appears`. |
| Logic | No triggers and no stored procedures. The database holds constraints. Python holds logic. |

### 3.3 Table definitions

This DDL is the source for the SQLAlchemy models and for migrations `0001` to `0004` (one per slice). The file applies cleanly on PostgreSQL 17. Tests then feed each rule a bad row and a good row (`tests/integration/test_db_rules*.py`).

```sql
CREATE TABLE users (
    id            uuid PRIMARY KEY,
    email         text        NOT NULL,
    password_hash text        NOT NULL,
    full_name     text,
    phone         text,
    role          varchar(32) NOT NULL DEFAULT 'PATIENT',
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_users_email           UNIQUE (email),
    CONSTRAINT ck_users_email_lowercase CHECK (email = lower(email)),
    CONSTRAINT ck_users_role            CHECK (role IN ('PATIENT', 'ADMIN'))
);

CREATE TABLE centres (
    id         uuid PRIMARY KEY,
    name       text        NOT NULL,
    location   text        NOT NULL,
    city       text,
    is_active  boolean     NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_centres_name_location UNIQUE (name, location)
);
CREATE INDEX ix_centres_city_active ON centres (lower(city)) WHERE is_active;

CREATE TABLE diagnostic_tests (
    id          uuid PRIMARY KEY,
    code        text        NOT NULL,
    name        text        NOT NULL,
    description text,
    is_active   boolean     NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_diagnostic_tests_code UNIQUE (code),
    CONSTRAINT uq_diagnostic_tests_name UNIQUE (name),
    CONSTRAINT ck_diagnostic_tests_code CHECK (code ~ '^[A-Z0-9_]{2,20}$')
);

CREATE TABLE centre_tests (
    centre_id    uuid          NOT NULL,
    test_id      uuid          NOT NULL,
    price        numeric(10,2) NOT NULL,
    currency     text          NOT NULL DEFAULT 'INR',
    is_available boolean       NOT NULL DEFAULT true,
    created_at   timestamptz   NOT NULL DEFAULT now(),
    updated_at   timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (centre_id, test_id),
    CONSTRAINT fk_centre_tests_centre_id FOREIGN KEY (centre_id) REFERENCES centres (id) ON DELETE RESTRICT,
    CONSTRAINT fk_centre_tests_test_id   FOREIGN KEY (test_id)   REFERENCES diagnostic_tests (id) ON DELETE RESTRICT,
    CONSTRAINT ck_centre_tests_price    CHECK (price BETWEEN 0.01 AND 99999999.99),
    CONSTRAINT ck_centre_tests_currency CHECK (currency ~ '^[A-Z]{3}$')
);
CREATE INDEX ix_centre_tests_test_id ON centre_tests (test_id);

CREATE TABLE bookings (
    id             uuid PRIMARY KEY,
    user_id        uuid          NOT NULL,
    centre_id      uuid          NOT NULL,
    test_id        uuid          NOT NULL,
    appointment_at timestamptz   NOT NULL,
    amount         numeric(10,2) NOT NULL,
    currency       text          NOT NULL,
    status         varchar(32)   NOT NULL DEFAULT 'PENDING',
    created_at     timestamptz   NOT NULL DEFAULT now(),
    updated_at     timestamptz   NOT NULL DEFAULT now(),
    CONSTRAINT fk_bookings_user_id     FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_bookings_centre_test FOREIGN KEY (centre_id, test_id)
        REFERENCES centre_tests (centre_id, test_id) ON DELETE RESTRICT,
    CONSTRAINT ck_bookings_amount   CHECK (amount BETWEEN 0.01 AND 99999999.99),
    CONSTRAINT ck_bookings_currency CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT ck_bookings_status   CHECK (status IN ('PENDING', 'CONFIRMED', 'FAILED', 'CANCELLED'))
);
CREATE INDEX ix_bookings_user_created ON bookings (user_id, created_at DESC, id DESC);
CREATE INDEX ix_bookings_centre_test  ON bookings (centre_id, test_id);
CREATE UNIQUE INDEX uq_bookings_active_slot
    ON bookings (user_id, centre_id, test_id, appointment_at)
    WHERE status IN ('PENDING', 'CONFIRMED');

CREATE TABLE payments (
    id                  uuid PRIMARY KEY,
    booking_id          uuid          NOT NULL,
    amount              numeric(10,2) NOT NULL,
    currency            text          NOT NULL,
    status              varchar(32)   NOT NULL DEFAULT 'PENDING',
    provider            text          NOT NULL DEFAULT 'SIMULATOR',
    provider_payment_id text,
    failure_reason      text,
    settled_at          timestamptz,
    created_at          timestamptz   NOT NULL DEFAULT now(),
    updated_at          timestamptz   NOT NULL DEFAULT now(),
    CONSTRAINT fk_payments_booking_id   FOREIGN KEY (booking_id) REFERENCES bookings (id) ON DELETE RESTRICT,
    CONSTRAINT ck_payments_amount       CHECK (amount BETWEEN 0.01 AND 99999999.99),
    CONSTRAINT ck_payments_currency     CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT ck_payments_status       CHECK (status IN ('PENDING', 'SUCCESS', 'FAILED')),
    CONSTRAINT ck_payments_settled      CHECK ((status = 'PENDING') = (settled_at IS NULL)),
    CONSTRAINT ck_payments_provider_ref CHECK (provider_payment_id <> '')
);
CREATE INDEX ix_payments_booking_id ON payments (booking_id);
CREATE UNIQUE INDEX uq_payments_live_per_booking
    ON payments (booking_id) WHERE status IN ('PENDING', 'SUCCESS');
CREATE UNIQUE INDEX uq_payments_provider_ref
    ON payments (provider, provider_payment_id) WHERE provider_payment_id IS NOT NULL;

CREATE TABLE webhook_events (
    id           uuid PRIMARY KEY,
    provider     text        NOT NULL,
    event_id     text        NOT NULL,
    payment_id   uuid,
    payload      jsonb       NOT NULL,
    outcome      varchar(32),
    detail       varchar(64),
    received_at  timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz,
    CONSTRAINT fk_webhook_events_payment_id FOREIGN KEY (payment_id) REFERENCES payments (id) ON DELETE RESTRICT,
    CONSTRAINT uq_webhook_events_provider_event UNIQUE (provider, event_id),
    CONSTRAINT ck_webhook_events_outcome CHECK (
        outcome IN ('APPLIED', 'IGNORED_NOOP', 'NEEDS_REVIEW', 'REJECTED')),
    CONSTRAINT ck_webhook_events_processed CHECK ((outcome IS NULL) = (processed_at IS NULL))
);
CREATE INDEX ix_webhook_events_payment_id ON webhook_events (payment_id);
```

`outcome` and `processed_at` are `NULL` only inside the transaction that handles the event. After the commit they are always set. `payment_id` stays `NULL` until the handler has locked the payment, and it stays `NULL` for an event about an unknown payment. The insert leaves it `NULL` on purpose, so it takes no lock on the payment. `detail` holds a short code for `REJECTED` and `NEEDS_REVIEW` outcomes (§6.5).

The DDL above leaves primary keys unnamed, and PostgreSQL then calls them `<table>_pkey`. The models and the migrations name them `pk_<table>` through the naming convention (§3.2). The schema-equivalence test keeps the two in step.

### 3.4 Index plan

| Query (access path) | Index | Endpoint |
|---|---|---|
| User by email | `uq_users_email` | `POST /auth/login` |
| User by ID | primary key | every authenticated request |
| Centre uniqueness | `uq_centres_name_location` | `POST /centres/` |
| Active centres in a city | `ix_centres_city_active` (partial) | `GET /centres/?city=` |
| Centre with its tests | primary keys of `centres` and `centre_tests` | `GET /centres/{id}` |
| Centres that offer a test | `ix_centre_tests_test_id` | `GET /centres/?test_id=` |
| Active centre-test at booking time | primary key of `centre_tests` | `POST /bookings/` |
| My bookings, newest first | `ix_bookings_user_created` (also the `user_id` foreign key index, and shaped for keyset pagination) | `GET /bookings/` |
| Duplicate booking guard and active-booking count | `uq_bookings_active_slot` (partial), `ix_bookings_user_created` | `POST /bookings/` |
| Foreign key check `centre_tests` → `bookings` | `ix_bookings_centre_test` | constraint enforcement |
| Live payment of a booking | `uq_payments_live_per_booking` (partial) | `POST /payments/`, `POST /bookings/{id}/cancel` |
| All payments of a booking, and the foreign key check on `payments.booking_id` | `ix_payments_booking_id` (a full index, because a partial index cannot serve the foreign key check) | `GET /bookings/{id}` |
| Provider reference conflict check (the webhook loads the payment by its primary key) | `uq_payments_provider_ref` (partial) | webhook |
| Webhook deduplication | `uq_webhook_events_provider_event` | webhook |
| Events of a payment | `ix_webhook_events_payment_id` | operations queries |

The `q` filter on `GET /centres/` uses `ILIKE` with escaped wildcards and reads the small catalog with a sequential scan. `# ponytail: sequential scan → pg_trgm GIN index when centres pass about 10,000 rows`.

The two operator queries scan their tables: stuck `PENDING` payments (§8.2, Q4) and `REJECTED` or `NEEDS_REVIEW` events (§6.5). `# ponytail: sequential scan → partial indexes on payments (created_at) WHERE status = 'PENDING' and on webhook_events (received_at) WHERE outcome IN ('REJECTED', 'NEEDS_REVIEW'), once either table passes about 1 million rows`.

### 3.5 Invariants

| Invariant | Enforced by |
|---|---|
| A booking's test is offered at its centre. | Composite foreign key to `centre_tests`. |
| Catalog history cannot disappear. | `ON DELETE RESTRICT` on every foreign key. |
| Prices and amounts lie between 0.01 and 99,999,999.99 and are never `NaN`. | `CHECK … BETWEEN`. |
| Currency is a three-letter upper-case code. | `ck_*_currency` on prices, bookings and payments. |
| Status values are valid. | Named `CHECK` on each status column. |
| A user holds one active booking per test, centre and start time. | `uq_bookings_active_slot`. |
| A centre has a unique name and location pair. | `uq_centres_name_location`. |
| A booking has at most one `PENDING` or `SUCCESS` payment. | `uq_payments_live_per_booking`. |
| A payment has `settled_at` if and only if it is not `PENDING`. | `ck_payments_settled`. |
| One provider payment ID maps to one payment, and it is never empty. | `uq_payments_provider_ref`, `ck_payments_provider_ref`. |
| The system processes a webhook event once. | `uq_webhook_events_provider_event`. |
| A webhook outcome is one of four values, and it is set together with `processed_at`. | `ck_webhook_events_outcome`, `ck_webhook_events_processed`. |
| Emails are unique and lower-case. | `uq_users_email`, `ck_users_email_lowercase`. |

The database cannot express these rules. Services and pure functions enforce them:

- Legal status transitions (§4).
- The appointment window, and the cap of active bookings per user.
- Ownership and roles.
- No cancellation while a live `PENDING` payment exists.
- A webhook changes only a payment that exists.
- A payment's amount and currency equal its booking's. A booking's amount and currency equal the price row that it copied.
- A `SUCCESS` payment goes with a `CONFIRMED` booking, or with a `CANCELLED` one after a cancellation (assumption A7). `settle()` sets both in one transaction.
- A committed webhook event has an outcome. Only a `REJECTED` event for an unknown payment has no `payment_id`.

### 3.6 Migrations, seed data and roles

- **Alembic** owns the schema. `Base.metadata.create_all()` is never used outside tests.
- Each migration starts from autogenerate. Then add the `CHECK` constraints and partial indexes by hand, because autogenerate can miss them.
- CI runs `alembic upgrade head` on an empty database. CI also runs `alembic check` to detect drift between the models and the migrations.
- Autogenerate cannot see some changes: `CHECK` expressions, partial-index predicates, and server defaults unless enabled. `env.py` sets `compare_type=True` and `compare_server_default=True`, and it enables Alembic's opt-in plugin `alembic.ext.checkconstraint_byname` through `autogenerate_plugins` (Alembic 1.20). A **schema-equivalence test** builds one database with `alembic upgrade head` and another with `metadata.create_all()`. It compares the constraint and index definitions from `pg_catalog`. Any difference fails the test.
- A **foreign-key index audit** runs as a test. It returns zero rows on the real schema. The query compares the leading columns of each index with the columns of each foreign key, so it handles composite keys. It skips partial and expression indexes, because they cannot serve the foreign key check. The audit was checked against five controls: dropping `ix_centre_tests_test_id`, `ix_bookings_user_created`, `ix_bookings_centre_test`, `ix_payments_booking_id` or `ix_webhook_events_payment_id` makes it flag the matching foreign key. The partial `uq_payments_live_per_booking` also leads with `booking_id`, and the query must not accept it as the index of the foreign key.

```sql
SELECT c.conrelid::regclass AS table_name, c.conname AS foreign_key
FROM pg_constraint AS c
WHERE c.contype = 'f'
  AND NOT EXISTS (
        SELECT 1 FROM pg_index AS i
        WHERE i.indrelid = c.conrelid
          AND i.indisvalid
          AND i.indpred IS NULL
          AND i.indexprs IS NULL
          AND (i.indkey::int2[])[0 : cardinality(c.conkey) - 1] @> c.conkey
          AND c.conkey @> (i.indkey::int2[])[0 : cardinality(c.conkey) - 1]);
```

- `scripts/seed.py` is idempotent. It inserts what is missing, matched by natural key, and never changes a row that exists. An edit by an admin therefore survives the next run. It creates the admin from `ADMIN_EMAIL` and `ADMIN_PASSWORD`, 5 centres, 12 diagnostic tests and about 30 prices, all in one transaction. The login form's own schema checks the credentials, so the admin can always sign in. An existing admin stays as it is. An account with that email that is not an admin stops the run, so nobody who signed up first becomes the admin. In production the seed refuses a placeholder or a password shorter than `PASSWORD_MIN_LENGTH`.
- `scripts/seed.py --scale N` adds N bookings on the seeded catalogue. It spreads them over N / 10 users, so each user holds about ten. The bookings carry a mix of statuses (60% `CONFIRMED`, 20% `PENDING`, 10% `FAILED`, 10% `CANCELLED`) and appointment times spread over the booking window. `CONFIRMED` and `CANCELLED` bookings get a `SUCCESS` payment (a cancelled booking keeps its payment, assumption A7). `FAILED` bookings get a `FAILED` payment. About half of the `PENDING` bookings get a `PENDING` payment. `--scale 100000` builds the 100,000-booking data set that §8.1 uses.
- **P2:** two database roles. `eve_owner` runs migrations. `eve_app` has data-manipulation rights only. An injection bug then cannot drop tables.

---

## 4. State machines

### 4.1 Booking

```mermaid
stateDiagram-v2
    [*] --> PENDING: booking created
    PENDING --> CONFIRMED: payment SUCCESS
    PENDING --> FAILED: payment FAILED
    PENDING --> CANCELLED: user cancels
    CONFIRMED --> CANCELLED: user cancels
    FAILED --> [*]
    CANCELLED --> [*]
```

### 4.2 Payment

```mermaid
stateDiagram-v2
    [*] --> PENDING: payment initiated
    PENDING --> SUCCESS: provider result
    PENDING --> FAILED: provider result
    SUCCESS --> [*]
    FAILED --> [*]
```

A payment stays `PENDING` only when the request sets `simulate=PENDING` (assumption A18). Without that field, `POST /payments/` creates the payment and settles it in the same transaction, so the client sees `SUCCESS` or `FAILED`.

The transition table is data, not `if` chains:

```python
BOOKING_TRANSITIONS: Mapping[BookingStatus, frozenset[BookingStatus]] = {
    BookingStatus.PENDING:   frozenset({CONFIRMED, FAILED, CANCELLED}),
    BookingStatus.CONFIRMED: frozenset({CANCELLED}),
    BookingStatus.FAILED:    frozenset(),
    BookingStatus.CANCELLED: frozenset(),
}
```

**ADR-09 changes `settle()`, not this table.** The alternative keeps the booking `PENDING` after a failed payment. In `settle()`, the `FAILED` branch then skips the booking transition. The partial unique index needs no change, because a `FAILED` payment is not live and a new payment can follow it. The cost: no code path sets a booking to `FAILED` any more, and the PDF lists `FAILED` as a booking state. This is why the recommendation keeps `FAILED` final.

| Transition | Trigger | Guard | Code path |
|---|---|---|---|
| none → `PENDING` | `POST /bookings/` | Active centre-test, valid window, under the booking cap, no duplicate | `BookingService.create` |
| `PENDING` → `CONFIRMED` | Payment `SUCCESS` | Booking is `PENDING` | `settlement.settle` |
| `PENDING` → `FAILED` | Payment `FAILED` | Booking is `PENDING` | `settlement.settle` |
| `PENDING` → `CANCELLED` | `POST /bookings/{id}/cancel` | No live `PENDING` payment | `BookingService.cancel` |
| `CONFIRMED` → `CANCELLED` | `POST /bookings/{id}/cancel` | None. Refund is out of scope. | `BookingService.cancel` |

Cancelling a `CANCELLED` booking returns `200` and changes nothing. Cancelling a `FAILED` booking returns `409`.

### 4.3 `settle()` decision table

`settle()` is the only code that changes a payment's status. The payment path and the webhook path both call it. The caller holds the booking lock. The function is pure. It returns a `Settlement` with an `outcome` and an optional `detail` code.

A result **matches** the stored reference when the result carries none or both are equal. When the stored payment has no reference yet and the result carries one, the payment stores it.

| Payment now | Booking now | Incoming result | Action | Outcome and detail |
|---|---|---|---|---|
| `PENDING` | `PENDING` | `SUCCESS` | Payment → `SUCCESS` (stores the reference). Booking → `CONFIRMED`. | `APPLIED` |
| `PENDING` | `PENDING` | `FAILED` | Payment → `FAILED`. Booking → `FAILED`. | `APPLIED` |
| `PENDING` | not `PENDING` | `FAILED` | Payment → `FAILED`. Booking unchanged. Unreachable while the cancel guard holds. | `APPLIED` |
| `PENDING` | not `PENDING` | `SUCCESS` | Payment → `SUCCESS`. Booking unchanged. Log at `ERROR`. Unreachable while the cancel guard holds. | `NEEDS_REVIEW`, `booking_not_pending` |
| `SUCCESS` or `FAILED` | any | same status, matching reference | None. | `IGNORED_NOOP` |
| `SUCCESS` or `FAILED` | any | same status, stored reference empty, result has one | Payment stores the reference. | `APPLIED` |
| `SUCCESS` or `FAILED` | any | same status, another reference | None. Log at `ERROR`. The provider names a second transaction. | `NEEDS_REVIEW`, `reference_mismatch` |
| `SUCCESS` | any | `FAILED` | None. Log at `ERROR`. | `NEEDS_REVIEW`, `terminal_conflict` |
| `FAILED` | any | `SUCCESS` | None. Log at `ERROR`. Money and booking disagree. | `NEEDS_REVIEW`, `terminal_conflict` |

A parametrised unit test walks every row. No database is involved. The webhook handler adds a fourth outcome, `REJECTED`, for events that it must not pass to `settle()` at all (§6.5).

---

## 5. API design

### 5.1 Conventions

| Topic | Rule |
|---|---|
| Base path | The root, with no version prefix. The PDF fixes `/payments/` and `/payments/webhook/`. A `/v1` prefix later is one argument on `include_router` (assumption A13). |
| Slashes | Every route answers in both spellings and never redirects (`TolerantRouter`, §2.3). Only the canonical spelling appears in `/docs`. Collections end with `/` (`/centres/`). Items do not (`/centres/{centre_id}`). |
| Resources | Plural nouns. Path IDs are typed `UUID`, so a malformed ID returns `422`. |
| Methods | `GET` reads. `POST` creates or runs a command. `PUT` sets a full value (the centre-test price). `PATCH` changes some fields. `DELETE` deactivates a catalog row and returns `204` (A14). |
| Commands | `POST /bookings/{id}/cancel` is the one non-CRUD command. `PATCH {"status": "CANCELLED"}` is rejected, because it lets a client write any status. |
| Field names | `snake_case`. Enum values are `UPPER_SNAKE`. Booleans start with `is_`. Timestamps are ISO 8601 with a UTC offset. Money is a decimal string. |
| Input schemas | `extra="forbid"`. Whitespace stripped, except in passwords. Length and range limits on every field. Text rejects control characters (Unicode category `Cc`, so `NUL`, TAB, LF and DEL too) and bidirectional override characters. PostgreSQL refuses `NUL` in `text`, and the others can disguise a name. A `description` also allows TAB and LF. Numeric fields match the database `CHECK` bounds. Separate `Create`, `Update` and `Read` models. A field that the server owns (`role`, `status`, `amount`, `user_id`) is never in an input model. The webhook model uses `extra="ignore"`, because a provider can add fields (§6.5). |
| Database errors | Every `CHECK` in §3.3 has a matching input rule in §5.6 or a server-side value. So a client cannot cause a `CHECK` violation. A violation that still happens is a bug and returns `500`. A `DataError` that reaches the handler (for example a value outside a column type) maps to `422 VALIDATION_ERROR`. The `q` filter escapes `%`, `_` and `\` before it builds the `ILIKE` pattern. |
| `PATCH` | Applies only the fields the client sent (`model_dump(exclude_unset=True)`). An empty body returns `422`. An explicit `null` returns `422` for every field that is not nullable. A shared `PatchModel` base enforces both rules, and each update model lists its nullable fields (`city`, `description`). Without the guard, `null` reached the database and gave a `500`. |
| Lists | `?limit=20&offset=0`. `limit` is 1–100. `offset` is 0–10,000. The response is `{"data": [...], "pagination": {"total", "limit", "offset"}}`. Order is fixed and has a tie-breaker (`id`). The total comes from a separate `count(*)` query, because `count(*) OVER()` scans every match and defeats `LIMIT`. |
| Status codes | `200` read, update or no-op. `201` create (with `Location` when the resource has a `GET` route). `202` accepted, result pending. `204` deactivate. `400` a body that the parser cannot read. `401`, `403`, `404`, `405`, `409`, `413`, `422`, `429`, `500`, `503`. |
| Content types | `application/json` in and out. `application/problem+json` for errors. `POST /auth/token` takes `application/x-www-form-urlencoded`. FastAPI 0.132 and newer reject a JSON body sent without `Content-Type: application/json`, so every example sets the header. A wrong `Content-Type` gives `422`, and not the `415` that RFC 9110 suggests. Malformed JSON gives `422`. Bytes that are not UTF-8 give `400`. |
| Caching | `NoStoreMiddleware` puts `Cache-Control: no-store` and `Pragma: no-cache` on every response in v1. The catch-all `500` handler adds them to its own response (§2.3). RFC 6749 §5.1 asks for both on token responses. P2 caching adds a cache header to catalog reads. |
| Idempotency | `GET`, `HEAD`, `PUT`, `PATCH` and `DELETE` are idempotent. Cancelling a cancelled booking is a no-op. `POST /payments/` is idempotent per booking while its payment is `PENDING` or `SUCCESS`. The webhook is idempotent per event ID. `POST /bookings/` is not idempotent. A repeat returns `409`. |
| Methods and headers | `TolerantRouter` answers `HEAD` wherever `GET` works (RFC 9110 §9.1) and both slash spellings of every path. |
| Limits | One cap of 64 KiB on every request body, through our own `BodyLimitMiddleware` (§2.3). The largest legitimate body is under 2 KiB. |
| CORS | Off by default. A setting holds an allow-list. |

### 5.2 Endpoint catalogue

**Query budget** = SQL statements on the success path after the authentication lookup. A test asserts it. An error path that must tell `403` from `404` runs one more statement.

| # | Method and path | Access | Success | Errors | Budget | Pri |
|---|---|---|---|---|---|---|
| 1 | `POST /auth/signup` | Public | `201` user | 409, 422, 429, 503 | 1 | P0 |
| 2 | `POST /auth/login` | Public | `200` token | 401, 422, 429, 503 | 1 | P0 |
| 3 | `POST /auth/token` | Public | `200` token (OAuth2 form, for Swagger) | 401, 422, 429, 503 | 1 | P0 |
| 4 | `GET /users/me` | User | `200` user | 401 | 0 | P0 |
| 5 | `GET /centres/` | Public | `200` page | 422 | 2 | P0 |
| 6 | `POST /centres/` | Admin | `201` centre | 401, 403, 409, 422 | 1 | P0 |
| 7 | `GET /centres/{centre_id}` | Public | `200` centre with tests | 404, 422 | 2 | P0 |
| 8 | `PATCH /centres/{centre_id}` | Admin | `200` centre | 401, 403, 404, 409, 422 | 2 | P1 |
| 9 | `DELETE /centres/{centre_id}` | Admin | `204` | 401, 403, 404, 422 | 1 | P1 |
| 10 | `GET /tests/` | Public | `200` page | 422 | 2 | P0 |
| 11 | `POST /tests/` | Admin | `201` test | 401, 403, 409, 422 | 1 | P0 |
| 12 | `GET /tests/{test_id}` | Public | `200` test | 404, 422 | 1 | P0 |
| 13 | `PATCH /tests/{test_id}` | Admin | `200` test | 401, 403, 404, 409, 422 | 2 | P1 |
| 14 | `DELETE /tests/{test_id}` | Admin | `204` | 401, 403, 404, 422 | 1 | P1 |
| 15 | `PUT /centres/{centre_id}/tests/{test_id}` | Admin | `201` created or `200` updated (centre-test) | 401, 403, 404, 422 | 1 | P0 |
| 16 | `POST /bookings/` | User | `201` booking | 401, 404, 409, 422 | 3 | P0 |
| 17 | `GET /bookings/` | User | `200` page (own) | 401, 422 | 2 | P0 |
| 18 | `GET /bookings/{booking_id}` | User (owner) | `200` booking with payments | 401, 403, 404, 422 | 1 | P0 |
| 19 | `POST /bookings/{booking_id}/cancel` | User (owner) | `200` booking | 401, 403, 404, 409, 422 | 4 (repeat: 2) | P0 |
| 20 | `POST /payments/` | User (owner) | `201`, `202` or `200` payment | 401, 403, 404, 409, 422 | 4 (replay: 2) | P0 |
| 21 | `GET /payments/{payment_id}` | User (owner) | `200` payment | 401, 403, 404, 422 | 1 | P0 |
| 22 | `POST /payments/webhook/` | Signature | `200` acknowledgement | 401, 413, 422, 503 | 8 (no-op: 6, duplicate: 1) | P0 |
| 23 | `GET /health/live` | Public | `200` | — | 0 | P1 |
| 24 | `GET /health/ready` | Public | `200` or `503` | 503 | 1 | P1 |

Every endpoint can also answer `400` (a body that cannot be parsed), `413` (a body over the cap), `500` and `503`. Every endpoint that reads the database can hit a lock, statement or pool timeout, which gives `503`. The OpenAPI document lists these four through shared default responses. Endpoints 1 to 3 add `429` when the login throttle fires (§7.2). Endpoints 20 and 22 also answer at `/payments` and `/payments/webhook` (no trailing slash) without a redirect, like every other route. The admin-only routers carry `dependencies=[Depends(require_admin)]` on the router. A new admin endpoint cannot ship without the check.

### 5.3 Error catalogue

All errors use RFC 9457 `application/problem+json`. Clients switch on `code`. The text in `detail` is not a contract and can change. Every `401` carries a `WWW-Authenticate` header (RFC 9110 §15.5.2).

```json
{
  "type": "urn:eve:problem:booking-not-payable",
  "title": "Booking is not payable",
  "status": 409,
  "detail": "Booking 7c1f0c1e has status CANCELLED. Only PENDING bookings can be paid.",
  "instance": "/payments/",
  "code": "BOOKING_NOT_PAYABLE",
  "request_id": "b6a1f7d2-3b0e-4c53-9a55-0d8f2f6c1a77"
}
```

A validation error adds an `errors` array:

```json
{
  "type": "urn:eve:problem:validation-error",
  "title": "Request validation failed",
  "status": 422,
  "code": "VALIDATION_ERROR",
  "instance": "/bookings/",
  "errors": [{"field": "body.appointment_at", "message": "Input should have timezone info", "type": "timezone_aware"}],
  "request_id": "b6a1f7d2-3b0e-4c53-9a55-0d8f2f6c1a77"
}
```

Each entry of `errors` maps `loc` to `field` (a dotted path), maps `msg` to `message` and keeps `type`. The handler never returns the rejected `input` or `ctx`, because those can hold a password.

| Code | HTTP | Raised when |
|---|---|---|
| `BAD_REQUEST` | 400 | The server cannot read the request body at all, for example a malformed form, bytes that are not UTF-8, or both `Content-Length` and `Transfer-Encoding`. |
| `NOT_AUTHENTICATED` | 401 | The `Authorization` header is missing or malformed. The response carries `WWW-Authenticate: Bearer`. |
| `INVALID_TOKEN` | 401 | The token has a bad signature, wrong algorithm or expired lifetime, or names an unknown user. `WWW-Authenticate: Bearer error="invalid_token"`. |
| `INVALID_CREDENTIALS` | 401 | The email or password is wrong. The response is identical for both. `WWW-Authenticate: Bearer`. |
| `WEBHOOK_SIGNATURE_INVALID` | 401 | The signature header is missing, malformed, wrong or stale. One code for all four, so an attacker learns nothing. `WWW-Authenticate: Signature realm="webhook"`. |
| `FORBIDDEN` | 403 | The role is too low, or the resource belongs to another user. |
| `VALIDATION_ERROR` | 422 | The path, query or body is invalid, including a malformed UUID, a `NUL` character, or a value that the database type refuses. |
| `APPOINTMENT_OUT_OF_WINDOW` | 422 | The appointment starts earlier than the minimum lead time or later than the maximum advance. |
| `SIMULATION_NOT_ALLOWED` | 422 | The request sets `simulate` while the setting forbids it. |
| `CENTRE_NOT_FOUND`, `DIAGNOSTIC_TEST_NOT_FOUND`, `CENTRE_TEST_NOT_FOUND`, `BOOKING_NOT_FOUND`, `PAYMENT_NOT_FOUND` | 404 | The ID is well formed and unknown. Public reads and bookings give the same answer for an inactive row. |
| `ROUTE_NOT_FOUND` | 404 | The path matches no route. |
| `METHOD_NOT_ALLOWED` | 405 | The path exists and the method does not. The response carries `Allow` with every method that the path serves (§2.3). |
| `EMAIL_ALREADY_REGISTERED` | 409 | The email exists (case-insensitive). |
| `DUPLICATE_CENTRE`, `DUPLICATE_DIAGNOSTIC_TEST` | 409 | A unique constraint on the catalog fails. |
| `DUPLICATE_BOOKING` | 409 | The user already holds an active booking for the same test, centre and start minute. |
| `BOOKING_LIMIT_REACHED` | 409 | The user already holds the maximum number of active future bookings (default 20). |
| `INVALID_BOOKING_STATUS` | 409 | The status transition is not in the transition table. |
| `BOOKING_NOT_PAYABLE` | 409 | The booking is not `PENDING`, or its appointment has started, and no live payment exists to replay. |
| `PAYMENT_IN_PROGRESS` | 409 | A cancellation meets a live `PENDING` payment. |
| `PAYLOAD_TOO_LARGE` | 413 | The request body exceeds 64 KiB. |
| `RATE_LIMITED` | 429 | The client exceeded a limit. The response carries `Retry-After`. |
| `SERVICE_UNAVAILABLE` | 503 | The database is unreachable, a lock, statement or pool timeout fired, or the hashing semaphore stayed full. The response carries `Retry-After`. |
| `INTERNAL_ERROR` | 500 | An unexpected error. The body has no internal detail. |

A webhook that fails business checks is not an error. It gets `200` with an outcome (§6.5). `errors.py` defines the codes once, as the members of one `ErrorCode` enum. Each `AppError` subclass names its member. `ProblemDetail.code` has the type `ErrorCode`, so the OpenAPI document lists every code. A unit test asserts that every member has a subclass and every subclass has one member.

### 5.4 Route design cards

Each card lists the exact steps of a critical endpoint. The **Tests** line names the abuse and edge cases that must pass.

#### `POST /auth/signup`

- **Input.** `email` (`EmailStr`, lower-cased, trimmed), `password` (15–128 characters, set by `PASSWORD_MIN_LENGTH`, never stripped), `full_name` (optional, 1–100), `phone` (optional, E.164). Extra fields return `422`, so `{"role": "ADMIN"}` never works.
- **Steps.** The `auth_gate` dependency counts the request against the per-IP limit and takes a hashing slot, on the event loop (§7.2). Normalise the password (NFC) and hash it with Argon2id in the thread pool. Run `INSERT INTO users … RETURNING *`. Commit. A violation of `uq_users_email` becomes `EMAIL_ALREADY_REGISTERED`. There is no pre-check `SELECT`, so two simultaneous signups cannot both pass.
- **Response.** `201` with `UserRead` (`id`, `email`, `full_name`, `phone`, `role`, `created_at`). There is no `Location`, because no route reads a user by ID. The hash never leaves the service.
- **Trade-offs.** A `409` reveals that an email exists. The per-IP throttle limits abuse. Password rules are length-only, as in NIST SP 800-63B-4. NIST also asks for a blocklist check. `# ponytail: no blocklist, because the 15-character minimum removes most common passwords → add a k-anonymity or top-10k list check`.
- **Tests.** Happy path. Duplicate email in different case. Password of 14 characters. Password with leading spaces keeps them. Extra `role` field. Response has no hash. Two concurrent signups: one `201`, one `409`. Throttle returns `429`.

#### `POST /auth/login` and `POST /auth/token`

- **Input.** `/auth/login` takes JSON: `email` (a plain string of 1–254 characters, lower-cased, not an `EmailStr`) and `password`. `/auth/token` takes the OAuth2 password form: `username` (the same rules) and `password`. Both call the same service method. `/auth/token` exists so that Swagger's *Authorize* button works (ADR-08).
- **Steps.** The `auth_gate` dependency runs on the event loop, before the request reaches the thread pool. It checks the throttle for the client IP and for the account (the SHA-256 key of the NFC-normalised, lower-cased email). Then it takes a hashing slot. Load the user by lower-cased email. Verify the password. When no user exists, verify against a fixed dummy hash, so both failures take the same time. On failure, count the failure. On success, clear the account counter. Create a token with `sub`, `iat`, `exp` (now + 30 minutes), `iss` and `aud`.
- **Response.** `200` with `{"access_token", "token_type": "bearer", "expires_in": 1800}`. The middleware adds `Cache-Control: no-store` and `Pragma: no-cache` (RFC 6749 §5.1).
- **Errors.** `401 INVALID_CREDENTIALS` for an unknown email and for a wrong password. `429 RATE_LIMITED` with `Retry-After` when a throttle fires. `503` at once when every hashing slot is taken. An email of more than 254 characters returns `422`.
- **Tests.** Happy path on both endpoints. Wrong password. Unknown email (the dummy hash runs). Token claims. Expired token rejected. Token with `alg: none` rejected. Token signed with another secret rejected. The 11th attempt after ten failures for one account returns `429`, also for an email that does not exist. Login through Swagger *Authorize*.

#### `POST /bookings/`

- **Input.** `centre_id`, `test_id`, `appointment_at` (`AwareDatetime`, truncated to the minute by the schema, and only ISO 8601 text: a JSON number is refused). `amount`, `status` and `user_id` are not accepted.
- **Steps.**
  1. Select the price and currency from `centre_tests` joined to `centres` and `diagnostic_tests`. Require `is_available`, and require both parents to be `is_active`. No row means `404 CENTRE_TEST_NOT_FOUND`.
  2. Verify the appointment window with the clock. The check compares aware datetimes and never converts them to UTC, because converting `9999-12-31T23:59:59-14:00` raises `OverflowError`. Failure means `422 APPOINTMENT_OUT_OF_WINDOW`.
  3. Count the user's active future bookings. At the cap (default 20) the answer is `409 BOOKING_LIMIT_REACHED`.
  4. Insert the booking with `status = 'PENDING'` and `amount` copied from step 1. Commit.
  5. A violation of `uq_bookings_active_slot` becomes `409 DUPLICATE_BOOKING`.
- **Response.** `201`, `Location` (a path built with `request.app.url_path_for`, so no client `Host` header reaches it and a later `/v1` prefix needs no change), `BookingRead`.
- **Concurrency.** Two identical requests: the unique index lets one succeed. A price change between step 1 and step 4 is harmless, because the booking stores the price it read. The cap is a soft limit. Two simultaneous requests can both pass the count. `# ponytail: soft cap → lock the user row with SELECT … FOR NO KEY UPDATE if the cap must be exact`.
- **Tests.** Happy path. Price snapshot survives a later price change. Unknown centre. Unknown test. Test not offered at the centre. Unavailable offering. Inactive centre. Past time. Naive datetime. More than 365 days ahead. Seconds are truncated. Duplicate. The 21st active booking. Extra `amount` field. No token. Concurrent duplicate. Query count.

#### `POST /bookings/{booking_id}/cancel`

- **Steps.** Lock the owned booking (§2.2): `SELECT … FROM bookings WHERE id = :id AND user_id = :user FOR NO KEY UPDATE`. On a miss, a plain read decides `404` or `403`. Status `CANCELLED`: return `200` unchanged. Transition check: `FAILED` gives `409 INVALID_BOOKING_STATUS`. `SELECT` for a `PENDING` payment: if found, `409 PAYMENT_IN_PROGRESS`. Update the status. Read the centre and test names for the response, because a locking query reads one table. Commit. That is four statements, and a repeat takes two.
- **Tests.** `PENDING` to `CANCELLED`. `CONFIRMED` to `CANCELLED`. Second cancel returns `200`. `FAILED` returns `409`. Another user's booking returns `403`, even while the owner holds the row lock. Unknown ID returns `404`. Malformed ID returns `422`. Live payment returns `409`. Threaded cancel against pay: one wins and the final state is consistent.

#### `POST /payments/`

- **Input.** `booking_id`. Optional `simulate`: `SUCCESS`, `FAILED` or `PENDING` (allowed only when the setting permits it). The client never sends an amount.
- **Steps.** One transaction (ADR-05). Lock the owned booking. Look for a live payment: when one exists, commit and return it (replay). Otherwise check that the booking is `PENDING` and that the appointment is in the future. Build a `PENDING` payment with the booking's amount. Ask the in-process simulator. Run `settle()` with its result. Commit: one flush writes the payment and the booking. Details in §6.1.
- **Response.** `201` (with `Location: /payments/{id}`) when this call created the payment and it settled. `202` when the payment is `PENDING`, whether this call created it or replayed it (`Location` only when this call created it). `200` when the call replays a `SUCCESS` payment. The body is `PaymentRead` with a nested `booking` (`id`, `status`).
- **Idempotency.** A retry for the same booking returns the booking's live payment (`SUCCESS` or `PENDING`) and never creates a second one. After a `FAILED` payment the booking is final, so a retry gets `409`, and `GET /bookings/{id}` shows the failure. A cancelled booking that holds a `SUCCESS` payment replays it with `200`.
- **Errors.** `403` other user's booking. `404` unknown booking. `409 BOOKING_NOT_PAYABLE` when the booking is `FAILED` or `CANCELLED`, or its appointment has started, and it has no live payment to replay.
- **Tests.** `SUCCESS` confirms the booking. `FAILED` fails it. Other user's booking. Unknown booking. Non-payable booking. Started appointment. Replay after success returns `200` and the same payment. Double submit yields one payment row. `simulate=PENDING` leaves both rows `PENDING`, and a webhook then settles them. Override blocked when the setting forbids it. The amount always comes from the booking. Ten concurrent requests yield one payment row.

#### `POST /payments/webhook/`

- **Steps.**
  1. `BodyLimitMiddleware` rejects a body over 64 KiB with `413`.
  2. An async dependency reads the raw bytes and verifies `X-Webhook-Signature` (§6.5). Failure: `401`. `WEBHOOK_SIGNATURE_REQUIRED=false` skips this step outside production.
  3. Parse the body with `model_validate_json`. A `ValidationError` becomes a `RequestValidationError`, so the client gets a normal `422`. (A raw `ValidationError` in a dependency returns `500`.) A body that fails validation is not an event yet, so nothing is stored. The log records the request ID and the failing field names.
  4. Begin one transaction. Run `INSERT INTO webhook_events … ON CONFLICT DO NOTHING RETURNING id` with `payment_id` set to `NULL`. No row: return `200 {"status": "duplicate"}`.
  5. Read the payment's booking ID. Unknown payment: mark the event `REJECTED` (`unknown_payment`), commit, return `200`.
  6. Lock the booking, then the payment (`FOR NO KEY UPDATE`, `populate_existing`).
  7. Check amount, currency and provider reference against the payment. A mismatch marks the event `REJECTED` (`amount_mismatch`, `currency_mismatch` or `provider_reference_conflict`), commits and returns `200`.
  8. Run `settle()`. Mark the event with the outcome and set `payment_id`. Commit.
  9. A deadlock or a serialization failure (SQLSTATE 40P01 or 40001) runs steps 4 to 8 again, up to three attempts. So does a unique violation on `uq_payments_provider_ref`: a concurrent event took the same reference, and the second run records `REJECTED` (`provider_reference_conflict`). A lock timeout (55P03) returns `503` at once, and the provider retries. A `DataError` returns `422`. Other database errors return `503`.
- **Response.** `200 {"status": "processed", "outcome": "...", "detail": ...}` or `200 {"status": "duplicate"}`.
- **No body parameter.** The handler declares none. FastAPI therefore never parses the body, and a bad signature returns `401` even when the JSON is invalid. `openapi_extra` documents the body, and `add_model_schemas` registers `WebhookPayload` in `components.schemas`, so every `$ref` in the document resolves (a test walks them all).
- **Tests.** In §9.3 (webhook group).

#### `PUT /centres/{centre_id}/tests/{test_id}`

- **Input.** `price` (decimal, 0.01 to 99,999,999.99, two places), `currency` (default `INR`), `is_available` (default `true`).
- **Steps.** One statement: a CTE that runs `INSERT … ON CONFLICT (centre_id, test_id) DO UPDATE SET price=…, currency=…, is_available=…, updated_at=now() RETURNING …, (xmax = 0) AS inserted` and joins the test's `code` and `name`. A foreign key violation becomes `CENTRE_NOT_FOUND` or `DIAGNOSTIC_TEST_NOT_FOUND`, chosen by the constraint name (`fk_centre_tests_centre_id` or `fk_centre_tests_test_id`).
- **Response.** `201` when the row was created and `200` when it was updated. RFC 9110 §9.3.4 requires `201` on create. The `xmax = 0` test tells the two apart.
- **Notes.** The same body gives the same state, so the call is idempotent. The centre and the test can be inactive. The price is stored and shows once both are active again. A price change does not touch existing bookings. In P2 it also bumps the catalog cache generation.

#### `GET /centres/`

- **Input.** Optional `city` (case-insensitive exact match), `test_id`, `q` (case-insensitive substring of `name`, `location` or `city`, with `%`, `_` and `\` escaped). `limit`, `offset`.
- **Steps.** `SELECT count(*)` over the active centres that match. Then one page query ordered by `name, id`. With `test_id` set, the query joins `centre_tests` (available rows only) and returns each centre's price for that test. Without it, the list carries no price.
- **Response.** `200` with a page of `CentreRead`. Inactive centres never appear. An admin restores one through `PATCH` with its known ID. `# ponytail: no admin list of inactive rows → add ?include_inactive for admins`.
- **Tests.** Each filter alone and combined. The city match ignores case. `%` and `_` match literally. The price appears only with `test_id`. The order is stable across pages. Inactive centres are hidden. Query count.

#### `GET /bookings/{booking_id}`

- **Steps.** One statement: `SELECT … FROM bookings LEFT OUTER JOIN payments … WHERE bookings.id = :id AND bookings.user_id = :user`, with the centre and test names joined in. One statement sees one snapshot, so the booking status and its payments always agree. On a miss, a plain existence read tells `404` from `403`.
- **Tests.** Own booking with and without payments. Another user's booking returns `403`. Unknown ID returns `404`. Booking and payment status agree while a webhook settles the payment. Query count.

#### `GET /bookings/`

- **Input.** Optional `status`. `limit`, `offset`.
- **Steps.** `SELECT count(*)` for the user (and status). Then one page query with a join to `centres` and `diagnostic_tests` for the names. The index serves both the filter and the order. For a user with few bookings, the planner sorts them in memory. For a heavy user (5,000 bookings in my check), page 1 reads the index in order. It stops at the limit, in under 0.2 ms. A deep page (offset 4,000) sorts about 4,000 rows in 3 to 5 ms. The offset cap of 10,000 bounds the worst case. Explicit `joinedload` options and `lazy="raise_on_sql"` on relationships prevent N+1.
- **Notes.** `# ponytail: offset pagination → keyset on (created_at, id) when a user can hold thousands of bookings`. The index already fits keyset.

### 5.5 OpenAPI documentation

- Title, version, and a description that explains the auth flow (sign up, log in, press *Authorize*).
- A tag with a description for each of: Auth, Users, Centres, Tests, Bookings, Payments, Health. The tag list order is the order in the UI.
- `OAuth2PasswordBearer(tokenUrl="auth/token", auto_error=False)` powers Swagger's *Authorize* button. It points at the form endpoint. `auto_error=False` lets our dependency raise the problem response.
- Stable `operationId` values through a `generate_unique_id_function` that returns `route.name`. Handler names are unique across all routers (`create_booking`, `list_bookings`, `create_payment`). A test enforces this. The older `{tag}-{name}` recipe raises `IndexError` on an untagged route.
- Every endpoint has a summary, a description, a request example and `responses` for each error status in the catalogue. The webhook documents its headers and body through `openapi_extra`.
- An automated test walks the OpenAPI document. It asserts that every operation has a summary, a unique `operationId` and a documented response for each status in §5.2.
- `/docs` and `/openapi.json` are on in `development` and `test`, and off in `production` by default (`DOCS_ENABLED`, §8.3).

### 5.6 Schemas

Contracts only. Every schema is a Pydantic v2 model in the feature's `schemas.py`. The model name shows its role: `Create` for input, `Update` for `PATCH`, `Read` for output.

| Schema | Fields | Rules |
|---|---|---|
| `SignupRequest` | `email`, `password`, `full_name?`, `phone?` | `extra="forbid"`. Email lower-cased. Password 15–128 characters, not stripped. |
| `LoginRequest` | `email`, `password` | `extra="forbid"`. `email` is a plain string of 1–254 characters, lower-cased. It is not an `EmailStr`, so an account that signup rejects (a seeded admin) can still log in. |
| `TokenRead` | `access_token`, `token_type`, `expires_in` | `token_type` is always `"bearer"`. |
| `UserRead` | `id`, `email`, `full_name`, `phone`, `role`, `created_at` | No hash. |
| `CentreCreate`, `CentreUpdate` | `name`, `location`, `city?`, `is_active?` (update only) | `name` 1–120, `location` 1–200, `city` optional, 1–80. Update fields are all optional. Update models extend `PatchModel`, which rejects `null` for every field that is not nullable (`city` is) and rejects an empty body. |
| `CentreRead` | `id`, `name`, `location`, `city` (can be empty), `is_active`, `price?` | `price` is set only when the list filters by `test_id`. |
| `CentreDetailRead` | `CentreRead` plus `tests: list[CentreTestRead]` | Available tests only. |
| `DiagnosticTestCreate`, `DiagnosticTestUpdate` | `code`, `name`, `description?`, `is_active?` (update only) | `code` matches `^[A-Z0-9_]{2,20}$`. `description` allows TAB and LF and can be `null` on update. Update models extend `PatchModel`. |
| `DiagnosticTestRead` | `id`, `code`, `name`, `description`, `is_active` | |
| `CentreTestWrite` | `price`, `currency="INR"`, `is_available=true` | `price` between 0.01 and 99,999,999.99 with at most two places. `currency` matches `^[A-Z]{3}$`, as in `WebhookPayload`, so `ck_centre_tests_currency` cannot fire. |
| `CentreTestRead` | `test_id`, `code`, `name`, `price`, `currency`, `is_available` | |
| `BookingCreate` | `centre_id`, `test_id`, `appointment_at` | `AwareDatetime` given as text. Seconds and microseconds set to zero. A JSON number is refused. |
| `BookingRead` | `id`, `user_id`, `centre`, `test`, `appointment_at`, `amount`, `currency`, `status`, `created_at` | `user_id` is the patient. `centre` and `test` are short objects (`id`, `name`). |
| `BookingDetailRead` | `BookingRead` plus `payments: list[PaymentSummary]` | |
| `PaymentSummary` | `id`, `amount`, `currency`, `status`, `failure_reason`, `settled_at` | |
| `PaymentCreate` | `booking_id`, `simulate?` | `simulate` is `SUCCESS`, `FAILED` or `PENDING`. |
| `PaymentRead` | `id`, `booking_id`, `amount`, `currency`, `status`, `failure_reason`, `settled_at`, `booking: {id, status}` | |
| `WebhookPayload` | `event_id`, `payment_id`, `status`, `amount?`, `currency?`, `provider_payment_id?`, `failure_reason?` | `extra="ignore"`. `status` is `SUCCESS` or `FAILED`. `event_id` and `provider_payment_id` have 1–128 characters. Every text field rejects control characters such as `NUL`. `currency` matches `^[A-Z]{3}$`. `failure_reason` is allowed only when `status` is `FAILED`. |
| `WebhookAck` | `status` (`processed` or `duplicate`), `outcome?`, `detail?` | |
| `ProblemDetail` | `type`, `title`, `status`, `detail?`, `instance`, `code` (an `ErrorCode`), `request_id`, `errors?` | The model behind every `responses=` entry. |
| `PatchModel` | base of every update model | `extra="forbid"`. A `before` validator rejects `null` for fields outside `nullable`. An `after` validator rejects an empty body. |

---

## 6. Payments and webhook

This section carries the most design risk. Tests check its concurrency claims against PostgreSQL 17 with real threads (§9.3).

### 6.1 Flows

The PDF describes two endpoints that both change payment and booking status. One function, `settle()`, changes them. Both endpoints call it.

**Pay a booking.** The whole flow is one short transaction. The simulator runs in the same process and does no I/O, so no lock spans a network call.

```mermaid
sequenceDiagram
    autonumber
    actor U as Patient
    participant A as API (PaymentService)
    participant DB as PostgreSQL
    participant S as PaymentSimulator
    U->>A: POST /payments/ with booking_id
    rect rgb(235, 243, 255)
    Note over A,DB: One transaction, short. The simulator does no I/O.
    A->>DB: SELECT booking WHERE id AND user_id FOR NO KEY UPDATE
    A->>DB: SELECT live payment (PENDING or SUCCESS)
    alt live payment exists
        A->>DB: COMMIT, lock released
        A-->>U: 200 or 202 with the live payment (replay)
    else no live payment
        A->>A: guards: booking is PENDING, appointment is in the future
        A->>S: charge(payment_id, override)
        S-->>A: SUCCESS, FAILED, or nothing when simulated as asynchronous
        A->>A: build the payment as PENDING with the booking's amount
        A->>A: settle(booking, payment, result), skipped in PENDING mode
        A->>DB: COMMIT: one flush writes INSERT payment and UPDATE booking
        A-->>U: 201 payment, or 202 when PENDING
    end
    end
```

In PENDING mode (`simulate=PENDING`), the payment row is inserted as `PENDING` and `settle()` does not run. The API answers `202`. The webhook settles the payment later.

**Order matters.** `settle()` receives a `PENDING` payment. Build the payment with its final status first, and `settle()` takes the same-status branch, returns `APPLIED` and leaves the booking `PENDING`. The `PENDING` payment also keeps the whole flow at four statements, because one flush writes the new payment and the changed booking.

**Pay decision table.** The order of the checks matters. The replay check comes first, so a booking that was cancelled after a successful payment still returns its payment.

| Live payment (`PENDING` or `SUCCESS`) | Booking | Appointment | Result |
|---|---|---|---|
| Exists, `SUCCESS` | any | any | `200` with that payment (replay) |
| Exists, `PENDING` | any | any | `202` with that payment (replay) |
| None | `PENDING` | in the future | Create the payment: `201`, or `202` in PENDING mode |
| None | `PENDING` | started | `409 BOOKING_NOT_PAYABLE` |
| None | not `PENDING` | any | `409 BOOKING_NOT_PAYABLE` |

**Receive a webhook.**

```mermaid
sequenceDiagram
    autonumber
    participant P as Provider (simulator script)
    participant A as API (PaymentService)
    participant DB as PostgreSQL
    P->>A: POST /payments/webhook/ with X-Webhook-Signature
    A->>A: verify HMAC on the raw body, 401 if invalid
    A->>DB: BEGIN, INSERT webhook_events (payment_id NULL) ON CONFLICT DO NOTHING
    alt no row returned, duplicate event_id
        A-->>P: 200 status duplicate
    else new event
        A->>DB: SELECT booking_id of the payment
        alt unknown payment
            A->>DB: UPDATE event REJECTED, COMMIT
            A-->>P: 200 outcome REJECTED, detail unknown_payment
        else known payment
            A->>DB: SELECT booking, then payment, FOR NO KEY UPDATE
            A->>A: check amount, currency and provider reference, then settle()
            A->>DB: UPDATE payment, booking and event outcome, COMMIT
            A-->>P: 200 status processed with outcome
        end
    end
```

### 6.2 Idempotency in four layers

| Layer | Mechanism | Stops |
|---|---|---|
| 1. Event gate | `UNIQUE (provider, event_id)`. `INSERT … ON CONFLICT DO NOTHING RETURNING id` runs in the same transaction as the state change. | The same event delivered twice, one after the other or at the same time. |
| 2. Payment no-op | `settle()` returns `IGNORED_NOOP` when the payment already has the incoming status and the same reference. | Different event IDs that carry the same result. |
| 3. Terminal guard | A conflicting terminal result or a second provider reference returns `NEEDS_REVIEW` and changes nothing. A `SUCCESS` for a booking that is not `PENDING` (unreachable) records the payment and returns `NEEDS_REVIEW`. | Late, reordered or contradictory events. |
| 4. Constraints | `uq_payments_live_per_booking`, `uq_payments_provider_ref`, `ck_payments_provider_ref`, the transition table. | Application bugs, manual SQL, races the code missed. |

**A webhook never creates anything.** It settles a payment that exists. It never inserts a payment or a booking (assumption A20). A duplicate delivery therefore cannot create a second payment.

**Why the gate and the state change share one transaction.** If processing fails, the rollback removes the event row. The provider's retry then runs the event again. If the gate committed alone, a crash leaves an event that is marked as seen and never applied.

**Why duplicates are cheap.** A duplicate of a finished event costs one statement, an index probe. It takes no booking lock. A duplicate that arrives while the first delivery still runs waits for that transaction. When the first delivery then times out on a held booking lock, the duplicate waits for that lock itself. Its worst wait is about two lock timeouts. `# ponytail: waiting duplicates hold a thread and a connection → fail fast with pg_try_advisory_xact_lock(hashtext(event_id)) if bursts of concurrent duplicates appear`.

**Concurrent duplicates.** The second `INSERT` waits for the first transaction to end. PostgreSQL blocks it on the unique index entry. If the first transaction commits, the second inserts nothing and answers `duplicate`. If the first rolls back, the second inserts the row and processes the event. *(Tests cover both cases with two sessions and with twenty concurrent deliveries.)* Keep the default READ COMMITTED isolation. Under REPEATABLE READ, the second insert fails with `40001`.

**Payment retries by the client.** `POST /payments/` is idempotent per booking while its payment is `PENDING` or `SUCCESS`. `uq_payments_live_per_booking` allows one such payment per booking. A second call finds that payment under the booking lock and returns it. After a `FAILED` payment the booking is final, and a retry gets `409`. A test runs 10 concurrent calls: one payment row, one settled response, nine replays.

### 6.3 Concurrency and locking

**Rule.** Lock the booking row first, then the payment row. Use `FOR NO KEY UPDATE` and `populate_existing=True` on every locking read (§2.2). On the user-facing paths, the lock query carries the owner predicate. The webhook inserts its event row before both locks. That insert waits only on the same event ID, and it leaves `payment_id` as `NULL`, so it takes no lock on the payment row. No lock cycle can form.

| Scenario | Mechanism | Result |
|---|---|---|
| Ten `POST /payments/` for one booking | The booking lock serialises them. | One payment row. The other nine replay it (`200` or `202`). |
| Pay against cancel (60 races with `SUCCESS`, 120 in `PENDING` mode) | The booking lock serialises them. Cancel refuses while a live `PENDING` payment exists. | Cancel first: pay returns `409 BOOKING_NOT_PAYABLE`. Pay first: cancel cancels a `CONFIRMED` booking, or (in `PENDING` mode) returns `409 PAYMENT_IN_PROGRESS`. Every final pair was valid. |
| PENDING mode, step by step | The live `PENDING` payment blocks cancel. A retry of `POST /payments/` replays it. The webhook settles it. | Cancel returns `409`. The retry returns `202`. The webhook returns `APPLIED`. Cancel then works. |
| Two webhooks, different event IDs, same result and reference | The gate lets both pass. The booking lock serialises them. | The second returns `IGNORED_NOOP`. |
| Ten webhooks, different event IDs, different references | Same. | One `APPLIED`, nine `NEEDS_REVIEW` (`reference_mismatch`). No error. |
| Two payments receive events with one reference at the same moment | The pre-check cannot see the other transaction's uncommitted update. The unique index stops the second update. The retry runs the pre-check again. | One `APPLIED`, one `REJECTED` (`provider_reference_conflict`). No error. Without the retry, 20 of 20 rounds raised `23505`. |
| A stranger cancels while the owner holds the booking lock | The owner predicate finds no row. No lock wait. | `403` in 3 to 22 ms across runs. Lock-then-check took about 1.8 s. |
| Two identical booking creates | `uq_bookings_active_slot`. | One `201`, one `409`. |
| Two webhooks for one payment, with `payment_id` set at insert and `FOR UPDATE` | The foreign key check and the row lock conflict. | Deadlock in 8 of 8 rounds. The design (`NULL` at insert, `FOR NO KEY UPDATE`) had 0 in 8. |

**Retries.** `retry_transient` (§2.5) runs the whole webhook transaction again after SQLSTATE 40001 or 40P01, or after a unique violation on `uq_payments_provider_ref`. It stops after three attempts. Three attempts recover every deadlock victim in the concurrency tests. Two events with one reference, delivered at the same moment, made the second transaction fail on the unique index in 20 of 20 rounds. The reference pre-check cannot see an uncommitted update. With the retry, the second run finds the committed reference and records `REJECTED`. A lock timeout (55P03) is not retried. The wait already took `lock_timeout` (2 s), and the provider's own retry is the right tool. The user-facing payment path does not retry. It returns `503` and the client retries.

**Timeouts** (P1, set on every connection): `statement_timeout` 5 s, `lock_timeout` 2 s, `idle_in_transaction_session_timeout` 15 s. A statement timeout (SQLSTATE 57014), a lock timeout (55P03), an idle-in-transaction timeout (25P03) and an exhausted connection pool (`TimeoutError`) map to `503 SERVICE_UNAVAILABLE` with `Retry-After: 1`. PostgreSQL 17 also offers `transaction_timeout`. It closes the connection when it fires, so the design does not use it. For the webhook, `503` makes the provider retry.

### 6.4 Failure modes and retry handling

| # | Failure | State left behind | Recovery |
|---|---|---|---|
| F1 | Crash or database error inside `pay` | Nothing. The one transaction rolls back. | The client retries and starts over. |
| F2 | The client disconnects after the commit | Payment settled | The client calls `POST /payments/` again. A `SUCCESS` payment replays with `200`. After a `FAILED` payment the booking is final, so the call gets `409 BOOKING_NOT_PAYABLE`, and `GET /bookings/{id}` shows the result. |
| F3 | The same webhook arrives twice at once | One event row | The second call gets `duplicate`. |
| F4 | Webhook processing fails midway (database down, timeout) | Rolled back. No event row. | Return `503`. The provider retries. |
| F5 | A deadlock, a serialization failure, or a second payment taking the same provider reference at the same moment (40P01, 40001, or 23505 on `uq_payments_provider_ref`) | Rolled back | The handler runs the transaction again, up to three attempts, then returns `503`. |
| F6 | Webhook for an unknown payment | Event stored as `REJECTED` (`unknown_payment`) | `200`. A retry cannot help: a payment row is committed before anyone learns its ID. An operator reads the row. |
| F7 | Bad signature or stale timestamp | Nothing | `401`. No retry expected. |
| F8 | Amount or currency mismatch, or a provider reference that belongs to another payment (`provider_reference_conflict`) | Event stored as `REJECTED`. `ERROR` log. | `200`. |
| F9 | Conflicting terminal result, second provider reference or cancelled booking | Event stored as `NEEDS_REVIEW` | `200`. An operator resolves it from the log. |
| F10 | Extra fields in the body | Ignored | None needed. |
| F11 | Oversized or invalid body | Nothing | `413` or `422`. |
| F12 | Hashing semaphore full (signup, login) | Nothing | `503` with `Retry-After`. |
| F13 | Redis down (P2) | No effect on correctness | The cache and the limiter fail open and log a warning. |

**Retry handling** (a PDF bonus item) has four parts:

1. The receiver is safe to retry, because of §6.2.
2. Status codes tell the provider what to do. `2xx` stops it. That includes `duplicate`, `REJECTED` and `NEEDS_REVIEW`, because a retry of the same bytes gives the same answer. `503` means "try again". `401`, `413` and `422` mean "this request is wrong". A provider that retries every non-`2xx` response, as Stripe does, retries those in vain. Each retry costs one cheap rejection.
3. The handler retries deadlocks, serialization failures and a provider-reference collision in process (§6.3).
4. `scripts/simulate_provider.py` retries `5xx` and network errors with exponential backoff and jitter. An integration test drives it against the running app.

Real providers behave the same way. Stripe treats every non-`2xx` response as a failure and retries for up to three days. Razorpay expects a `2xx` within 5 seconds, then retries for 24 hours. Our handler finishes in a few milliseconds. When another transaction holds the booking lock, the wait ends after `lock_timeout` (2 s) with a `503`. A duplicate that arrives during that wait waits behind the first delivery and then for the lock. Its worst case is about two lock timeouts (4 s). That is still inside the 5-second budget. A wait for a pool connection (`pool_timeout`, 10 s) can go past the budget when the service is overloaded. The provider then retries, and the event gate makes the retry safe. The handler does not retry a lock timeout in process.

There is no server-side retry queue (ADR-14).

### 6.5 Webhook contract

```http
POST /payments/webhook/ HTTP/1.1
Content-Type: application/json
X-Webhook-Signature: t=1790000000,v1=afce76fa350828580fc1b9c5ad8784b180b096922bb87e29b10d108a7b51b385
```

```json
{"event_id":"evt_01J8ZQ4M3K7W","payment_id":"0d4b7a52-3f1e-4d0a-9b8c-2f6a1c7e9d10","status":"SUCCESS","provider_payment_id":"sim_0d4b7a523f1e4d0a9b8c2f6a1c7e9d10","amount":"499.00","currency":"INR"}
```

The signature above is real. It uses the secret `whsec_demo_only_not_a_real_secret_0123456789`, the time `t=1790000000` and the body exactly as shown (compact, no spaces after `:` or `,`). Python's `hmac` and the `openssl` command in the recipe below both produce it. A unit test reproduces it. The signature covers the raw bytes, so any re-formatting of the JSON breaks it.

| Item | Rule |
|---|---|
| Signature | `v1 = hex(HMAC_SHA256(secret, f"{t}." + raw_body))`. `t` is a Unix time in seconds. |
| Verification | Parse the header. Require `abs(now - t) <= 300`. Compare bytes with `hmac.compare_digest`. It raises `TypeError` on a non-ASCII `str`. Verify the raw bytes before any JSON parsing. |
| Required by default | `WEBHOOK_SIGNATURE_REQUIRED` is `true`. Setting it to `false` skips the check for Swagger experiments. Production refuses to start with `false` (ADR-06). |
| Secret | `WEBHOOK_SECRET`, at least 32 bytes, from the environment. |
| Required fields | `event_id` (1–128 characters, the idempotency key, unique per provider), `payment_id` (UUID), `status` (`SUCCESS` or `FAILED`). |
| Optional fields | `amount` (decimal string), `currency`, `provider_payment_id` (1–128 characters), `failure_reason` (only with `FAILED`, because a payment that succeeded has nothing to explain). When `amount` or `currency` is present, the receiver compares it with its own payment row. Every text field rejects `NUL` and other control characters. |
| Unknown fields | Ignored. A provider can add fields without breaking us. |
| Stored payload | The `payload` column stores the validated fields (`model_dump(mode="json")`), not the raw body. Unknown fields are dropped and text fields reject `NUL`, so the `jsonb` insert cannot fail on `\u0000`. |
| Trust | The payload names only `payment_id`. The receiver derives the booking from its own records. It ignores any booking ID in the payload. |
| Response | `200 {"status": "processed", "outcome": "<outcome>", "detail": "<code or null>"}` or `200 {"status": "duplicate"}`. |
| `REJECTED` details | `unknown_payment`, `amount_mismatch`, `currency_mismatch`, `provider_reference_conflict`. The event is stored and nothing else changes. |
| `NEEDS_REVIEW` details | `terminal_conflict`, `reference_mismatch`, `booking_not_pending` (§4.3). |
| Errors | Problem format. `401` signature. `413` body too large. `422` body invalid: nothing is stored, and the log names the failing fields. `503` a database error that a retry can fix, such as a lock timeout. |
| Reused event ID | The gate answers `duplicate` for a reused `event_id`, even when the body differs. `# ponytail: no payload comparison → store a body hash and log or return 409 when it differs, if a provider reuses IDs` |
| Secret rotation | `# ponytail: one secret → accept a list of secrets during rotation` |
| Extra providers | `# ponytail: one provider (SIMULATOR) → route per provider: /payments/webhook/{provider}/` |
| Amount optional | `# ponytail: amount and currency optional to keep the contract minimal → make them required when the real provider always sends them` |

Every signed event with a valid body is stored, whatever its outcome. A body that fails validation is not an event yet. It gets `422`, nothing is stored, and the log names the failing fields. The `webhook_events` table is the audit trail. An operator finds every event that needs attention with `WHERE outcome IN ('REJECTED', 'NEEDS_REVIEW')`.

Manual test with `curl`:

```bash
# <PAYMENT_ID> belongs to a payment that you created with "simulate": "PENDING".
# The secret is the one that the API container uses. Without Docker, read WEBHOOK_SECRET from .env.
secret=$(docker compose exec -T api printenv WEBHOOK_SECRET)
: "${secret:?the api container is not running}"
id=$(date +%s%N)
body='{"event_id":"evt_'"$id"'","payment_id":"<PAYMENT_ID>","status":"SUCCESS","provider_payment_id":"sim_'"$id"'"}'
ts=$(date +%s)
sig=$(printf '%s.%s' "$ts" "$body" | openssl dgst -sha256 -hmac "$secret" -hex | sed 's/^.* //')
curl -X POST http://localhost:8000/payments/webhook/ -H "Content-Type: application/json" \
     -H "X-Webhook-Signature: t=$ts,v1=$sig" -d "$body"
```

The script does the same and adds duplicates and retries. It creates a fresh event ID for every run, and `--repeat` sends that event again. It reads the secret from `--secret`, the `WEBHOOK_SECRET` variable or `.env`, and it never prints it. It retries a network error, a `5xx`, `408` and `429` with exponential backoff and full jitter. It signs every attempt afresh over the same bytes. It stops at once on a `2xx` or on any other `4xx`:

```bash
uv run python -m scripts.simulate_provider --payment-id <PAYMENT_ID> --result SUCCESS --repeat 5 --concurrent
```

### 6.6 Upgrade path: a real payment gateway

The simulator returns its result without I/O, so §6.1 runs in one transaction. A real gateway needs a network call. The lock must not span that call. The design then becomes two-phase. This variant was built and tested. With an in-process simulator it creates a stuck `PENDING` state, so the service does not ship it (ADR-05).

```mermaid
sequenceDiagram
    autonumber
    actor U as Patient
    participant A as API (PaymentService)
    participant DB as PostgreSQL
    participant G as Real gateway
    U->>A: POST /payments/ with booking_id
    rect rgb(235, 243, 255)
    Note over A,DB: Transaction 1, short
    A->>DB: lock owned booking, check status, find live payment
    A->>DB: INSERT payment PENDING, COMMIT, lock released
    end
    A->>G: charge, with the payment ID as idempotency key, no transaction open
    G-->>A: SUCCESS, FAILED, or pending
    rect rgb(235, 255, 243)
    Note over A,DB: Transaction 2, short
    A->>DB: lock booking, then payment, with populate_existing
    A->>A: settle(booking, payment, result)
    A->>DB: UPDATE payment and booking, COMMIT
    end
    A-->>U: 201 payment, or 202 when pending
```

The upgrade needs four additions:

1. **Resume logic.** A crash between the two transactions leaves a `PENDING` payment and a `PENDING` booking. A replay that finds a stale `PENDING` payment asks the gateway for its result, using the same idempotency key, and settles it.
2. **A reconciler job (P2).** It scans `PENDING` payments older than 15 minutes and settles them the same way. This is where Celery or a scheduler earns its place.
3. **`populate_existing` discipline.** Transaction 2 must re-read the booking and the payment under the lock. Without it, a late `FAILED` result can overwrite a `SUCCESS` payment and a `CONFIRMED` booking. The design already applies the rule to every locking read.
4. **More tests.** A cancel between the two transactions (the guard refuses it). A webhook that lands between them. A gateway timeout. A crash after transaction 1.

The estimate is about three more hours of work (ADR-05). Nothing else in the schema or the webhook path changes, because `settle()` and the inbox stay the same.

---

## 7. Security

### 7.1 Threat model

**Assets.** Patient identity and contact data. Booking history (it shows which medical tests a person booked). Payment integrity. Admin control of the catalog and prices. The JWT secret and the webhook secret. Availability.

**Trust boundaries.**

| # | Boundary | Trust |
|---|---|---|
| TB1 | Internet client → API | None. Every input is hostile. |
| TB2 | Payment provider → webhook | Must prove identity on every request. |
| TB3 | API → PostgreSQL | Trusted. Least privilege is a P2 item. |
| TB4 | API → Redis | Trusted network. Optional (P2). |
| TB5 | Build pipeline → container image | Supply chain. Locked and audited dependencies. |

**STRIDE over the boundaries.**

| Threat | Example | Mitigation |
|---|---|---|
| **S**poofing | A forged webhook marks a booking as paid. | HMAC signature on the raw body, required by default and in production. Timestamp tolerance. Event dedupe (§6.5). |
| **S**poofing | A forged or stolen JWT. | Secret of at least 32 bytes. Pinned algorithm. Required `exp`. Short lifetime. |
| **S**poofing | Credential stuffing on login. | Argon2id. Identical error for unknown email and wrong password. Throttle per account and per IP (P1). |
| **T**ampering | The client sends `amount`, `status` or `role`. | `extra="forbid"`. The server owns these fields. |
| **T**ampering | A webhook carries a changed amount. | The receiver compares amount and currency with its own payment row and stores a `REJECTED` event. |
| **T**ampering | A second provider reference for one payment. | `settle()` returns `NEEDS_REVIEW`. `uq_payments_provider_ref` blocks reuse across payments. |
| **T**ampering | SQL injection. | ORM and bound parameters only. No string-built SQL. LIKE wildcards are escaped. |
| **R**epudiation | A user denies a booking or a cancellation. | One structured log event per status change, with `user_id` and `request_id`. The inbox stores the validated fields of every signed event that has a valid body. |
| **I**nformation disclosure | `password_hash` in a response. | `response_model` on every endpoint. |
| **I**nformation disclosure | Stack traces or SQL text in errors. | A generic `500` body. Details stay in the log. |
| **I**nformation disclosure | A validation error echoes the rejected input, for example a password. | The handler returns only `loc`, `msg` and `type`. It drops `input` and `ctx`. |
| **I**nformation disclosure | Reading another user's booking. | Ownership check in the service (`403`). |
| **I**nformation disclosure | Secrets or personal data in logs, including database error text and exception locals. | Redaction processor. No raw bodies. `show_locals=False`. A processor reduces database errors to class, SQLSTATE and constraint (§8.2). |
| **I**nformation disclosure | A shared cache stores a token or a booking. | `NoStoreMiddleware` sets `Cache-Control: no-store` on every response. The `500` handler sets it on its own response (§2.3). |
| **D**enial of service | Login flood (each hash costs CPU and 64 MiB). | Per-IP and per-account throttle. Non-blocking hashing slots cap concurrent hashes and answer `503` at once when all are taken. Nothing waits in a pool thread. |
| **D**enial of service | Huge bodies, unbounded lists, slow queries. | Body cap. `limit` and `offset` caps. `statement_timeout`. |
| **D**enial of service | A stuck transaction holds a lock. | Short transactions. `lock_timeout`. `idle_in_transaction_session_timeout`. Services end their transaction before they return. |
| **D**enial of service | A stranger ties up a booking row. | The owner predicate: a stranger never waits on or takes the owner's lock. |
| **E**levation of privilege | Signup with `role: ADMIN`. | Role is not an input field. |
| **E**levation of privilege | A new admin endpoint without a check. | The router carries `require_admin`. A test lists every route and its access level. |
| **E**levation of privilege | A tampered token claim. | The token holds no role. The service reads the role from the database. |
| **E**levation of privilege | A default admin password in production. | The seed refuses the development password when `ENVIRONMENT=production`. |

### 7.2 Authentication and authorization

| Topic | Decision |
|---|---|
| Password hashing | Argon2id through `pwdlib`. The hash string stores its own parameters, so cost can rise later. The library default (Argon2id, 64 MiB, 3 passes, 4 lanes) costs 55 to 67 ms per hash on an idle 8-core developer machine. It is above the OWASP minimum (19 MiB, 2 passes, 1 lane), which costs 22 to 25 ms here. We keep the default. |
| Password input | NFC-normalised, then hashed. Never stripped, so a leading or trailing space is part of the secret. Two Unicode forms of one visible password give one hash. |
| Hashing slots | A `threading.BoundedSemaphore` (`HASH_MAX_CONCURRENCY`, default 4) holds the slots. The `hash_slot` dependency is an `async def`. It takes a slot with `acquire(blocking=False)` on the event loop and releases it after the handler returns. A full semaphore answers `503 SERVICE_UNAVAILABLE` with `Retry-After` at once. Four hashes use about 256 MiB at peak. On the 8-core check machine, hashes did not run faster in parallel. One took 55 to 67 ms, and four at once took 230 to 240 ms each. The capacity stayed near 16 hashes per second. More slots add memory use and latency, and no speed. A semaphore that **waits** inside a sync handler holds a pool thread. In the check, a burst of 300 logins delayed `/health/live` by 1.8 s. With the non-blocking slot the worst delay was 0.02 s. |
| Password rules | 15 to 128 characters. No composition rules. NIST SP 800-63B-4 sets 15 as the minimum for a password used alone and asks for a blocklist check. (`# ponytail: no blocklist → k-anonymity or top-10k list`.) The minimum is a setting. |
| Auth throttle (P1) | The `limits` library with in-memory storage and a moving window. The `auth_gate` dependency checks it on the event loop, before the request reaches a pool thread. **Per account:** `LOGIN_MAX_FAILURES` failed logins (default 10) in `LOGIN_FAILURE_WINDOW_MINUTES` (default 15). The key is the SHA-256 of the NFC-normalised, lower-cased email. Case variants share one counter, and an unknown email behaves like a known one. **Per IP:** `AUTH_IP_LIMIT_PER_MINUTE` requests across signup, login and token. The default is 30 in production and 300 in development and test, because host-local Docker clients share one bridge address. An exceeded limit returns `429 RATE_LIMITED` with `Retry-After`. A good login clears the account counter. An email of more than 254 characters returns `422` before the gate runs, and a body over 64 KiB returns `413`. |
| Throttle trade-off | An attacker can slow down one victim's login for 15 minutes by sending bad passwords for that email. The attacker cannot read data or reset the password. The per-IP limit bounds the effort. `limits` keeps an empty entry for every key that it has seen, so memory grows with the number of distinct emails. The fixed-size key and the per-IP limit slow that growth. `# ponytail: in-memory counters are per process and only grow → use Redis storage (limits[redis], with expiry) before public exposure or with more than one worker`. |
| Access token | HS256 JWT through PyJWT. Claims: `sub` (a string), `iat`, `exp`, `iss`, `aud`. Lifetime 30 minutes. `decode` pins `algorithms=["HS256"]`, passes `issuer` and `audience`, and requires `exp`, `sub`, `iss` and `aud`. Common practice is 5 to 15 minutes. We use 30 because v1 has no refresh token. The lifetime is a setting. |
| Token secret | `JWT_SECRET`, at least 32 bytes (RFC 7518 section 3.2). The start-up check rejects short or placeholder values outside `development`. PyJWT runs with `enforce_minimum_key_length` on. |
| Transport | `Authorization: Bearer <token>`. No cookies, so there is no CSRF surface. |
| Role | `PATIENT` or `ADMIN`, stored in `users.role`. Never inside the token. |
| User lookup | One primary-key read per request. A changed role takes effect at once. |
| Authorization | Roles use `require_admin` on the router. Ownership uses the owner predicate inside the service query (§2.2). Admins get no special booking access in v1. |
| Timing | Unknown email: verify against a dummy hash, so the response time matches a wrong password. Start-up creates the dummy hash with the real hasher. A fake string raises `UnknownHashError`. |
| Refresh and revocation | None in v1 (assumption A11). A stolen token works for at most 30 minutes. Upgrade: rotating refresh tokens and a `jti` deny-list in Redis. |
| Secrets | `.env` is git-ignored. `SecretStr` keeps secrets out of `repr` and logs. The image contains no secrets. Compose supplies development-only defaults, and the start-up check rejects them when `ENVIRONMENT=production`. |
| Response headers | `NoStoreMiddleware` adds `Cache-Control: no-store` and `Pragma: no-cache` to every response that passes through it. The catch-all `500` handler adds the same two headers, because that response bypasses the middleware (§2.3). The reverse proxy adds HSTS and `X-Content-Type-Options`. |
| TLS | A reverse proxy terminates TLS. `fastapi run` enables proxy headers by default, but Uvicorn trusts them only from `127.0.0.1` or from `FORWARDED_ALLOW_IPS`. Set that variable to the proxy address or network. **Never `*`.** Production refuses to start with `*`, because `Settings` reads the variable too. With `*`, Uvicorn takes the leftmost `X-Forwarded-For` entry. The client writes that entry, so a client chooses its own IP and its own throttle key. With the proxy address listed, Uvicorn returns the address that the proxy appended. |

### 7.3 OWASP API Security Top 10 (2023)

| Risk | Exposure in this API | Control |
|---|---|---|
| API1 Broken Object Level Authorization | Booking and payment IDs in paths and bodies. | Owner predicate in every service query. Test for each endpoint. |
| API2 Broken Authentication | Signup, login, token handling. | Argon2id, pinned JWT algorithm, expiry, generic errors, throttle per account and per IP. |
| API3 Broken Object Property Level Authorization | Mass assignment of `role`, `status`, `amount`. Leaking `password_hash`. | Separate input and output models. `extra="forbid"`. `response_model`. |
| API4 Unrestricted Resource Consumption | Lists, bodies, Argon2 cost, webhook floods. | Caps, timeouts, hashing semaphore, throttle, cheap duplicate path. |
| API5 Broken Function Level Authorization | Admin endpoints for the catalog. | `require_admin` on the router. A route-inventory test. |
| API6 Unrestricted Access to Sensitive Business Flows | Booking spam. Payment abuse. | Duplicate-booking index. Cap of 20 active bookings per user. Idempotent payment. Other rate limits are P2 (§8.4). Slot hoarding is not a risk in v1, because centres have unlimited capacity (A4). |
| API7 Server Side Request Forgery | The API fetches no user-supplied URL. | Not applicable. The design adds no outbound fetch. |
| API8 Security Misconfiguration | Debug output, open CORS, default secrets, open docs, unsigned webhooks. | CORS off. `ENVIRONMENT` has no default. Start-up validation. Docs off, simulator override off and signature required in production. Non-root container. Generic `500`. |
| API9 Improper Inventory Management | Undocumented or forgotten endpoints. | One version. The OpenAPI document is the inventory. The route-inventory test fails on an unknown route. |
| API10 Unsafe Consumption of APIs | The webhook is third-party input. | Signature, schema validation, amount check, trust only our own records. |

### 7.4 Abuse cases and their tests

Each abuse case is an automated test. The list grows as new cases appear.

| # | Abuse case | Expected result |
|---|---|---|
| AB1 | `POST /bookings/` without a token | `401`, `WWW-Authenticate: Bearer` |
| AB2 | A patient calls `POST /centres/` | `403`, no row created |
| AB3 | User B cancels user A's booking | `403`, booking unchanged |
| AB4 | User B pays user A's booking | `403`, no payment row |
| AB5 | User B reads user A's payment | `403` |
| AB6 | Signup with `"role": "ADMIN"` | `422` |
| AB7 | Booking with `"amount": 1` | `422` |
| AB8 | Webhook with a missing or wrong signature | `401`, no state change |
| AB9 | Webhook replayed with an old timestamp | `401` |
| AB10 | Webhook with a changed amount | `200`, outcome `REJECTED` (`amount_mismatch`), no state change |
| AB11 | The same webhook 20 times at once | One `processed`, nineteen `duplicate`, one event row |
| AB12 | A `FAILED` event after a `SUCCESS` event | `200`, outcome `NEEDS_REVIEW` (`terminal_conflict`), booking stays `CONFIRMED` |
| AB13 | Webhook for an unknown payment | `200`, outcome `REJECTED` (`unknown_payment`), event stored, redelivery is `duplicate` |
| AB14 | JWT with `alg: none`, a wrong secret, or an expired lifetime | `401` |
| AB15 | A malformed UUID in a path | `422` |
| AB16 | Any request body over 64 KiB, with or without `Content-Length`. Example: a login with a 100,000-character email. | `413` problem response |
| AB17 | SQL metacharacters and `%` or `_` in the `q` filter | Normal response, tables intact, wildcards matched literally |
| AB18 | `limit=100000` | `422` |
| AB19 | Login with an unknown email and login with a wrong password | Identical `401` bodies |
| AB20 | Pay a cancelled booking that has no payment | `409 BOOKING_NOT_PAYABLE` |
| AB21 | Eleven bad logins for one email in 15 minutes | `429` with `Retry-After` on the eleventh attempt |
| AB22 | More than the per-IP limit of auth requests in one minute from one IP | `429` |
| AB23 | Ten events with different IDs and different provider references, at once | One `APPLIED`, nine `NEEDS_REVIEW` (`reference_mismatch`), no `500` |
| AB24 | One provider reference on a second payment, also when both events arrive at the same moment | `200`, outcome `REJECTED` (`provider_reference_conflict`), no `500` |
| AB25 | A password or a name with a control character, a bidirectional override or a lone surrogate (`\ud800` in the JSON), a 200-character password, or a 10,000-character email | `422`. The throttle key is a fixed-size hash for every email that reaches the gate. |
| AB26 | Start-up in production with `WEBHOOK_SIGNATURE_REQUIRED=false`, a short secret, `FORWARDED_ALLOW_IPS=*` or a missing `ENVIRONMENT` | The application refuses to start |
| AB27 | User B cancels user A's booking while user A holds its row lock | `403` at once, no wait |
| AB28 | A database constraint error and an unhandled exception in a request, run against a real `fastapi run` process | The log, standard output and standard error hold the constraint name, and no email, hash, query string or other row value |
| AB29 | The 21st active booking for one user | `409 BOOKING_LIMIT_REACHED` |
| AB30 | `simulate` in a request while `ENVIRONMENT=production` | `422 SIMULATION_NOT_ALLOWED` |
| AB31 | Pay a booking whose appointment has started | `409 BOOKING_NOT_PAYABLE` |
| AB32 | A signed webhook body with a bad field (`status` `REFUNDED`, a missing `event_id`, `NUL` in any text field, or a `failure_reason` on a `SUCCESS` event) | `422`, no event row, the log names the field |
| AB33 | `PATCH` with `{"name": null}` or `{}` | `422`, and no `500` |
| AB34 | `HEAD` on a `GET` route | `200` with no body |
| AB35 | A client sends `X-Forwarded-For: 6.6.6.6, <real address>` through the proxy | The throttle keys on the address that the proxy appended |
| AB36 | A request with `Content-Length: 5` and `Transfer-Encoding: chunked`, then a 200 KB body, sent to a server that runs the h11 parser | `400`, and the application never runs |

### 7.5 Privacy notes (informational, not legal advice)

The service stores an email address, an optional name, an optional phone number and the tests a person booked. Together these reveal health information. India's Digital Personal Data Protection Act 2023 and its Rules (notified 13 November 2025) apply. The Board provisions are in force. Most duties (notice, security safeguards, breach reporting, erasure, children's data) take effect about 13 May 2027. Ask counsel before you serve real patients.

- **Notice and consent.** A plain-language notice must list the data and the purpose. Withdrawing consent must be as easy as giving it. v1 has no consent record.
- **Purpose limitation.** The service uses personal data only to run bookings and payments.
- **Retention.** The Rules ask for erasure after withdrawal. They also require some logs to be kept for one year. Counsel must settle this tension. v1 logs IDs only.
- **Breach handling.** The operator must tell affected people and the Board and send a detailed report within 72 hours. v1 has no runbook for this.
- **Children.** Users under 18 need verifiable parental consent. v1 does not check age.

---

## 8. Cross-cutting concerns

### 8.1 Performance

**Budgets.** These are targets. [perf.md](perf.md) reports the measurements.

| Measure | Target |
|---|---|
| Response time, p95, all endpoints except signup, login and token | Under 100 ms at 100,000 bookings, one worker, developer laptop |
| Signup, login and token | About 100–200 ms, dominated by Argon2 |
| One SQL statement | Under 10 ms |
| Statements per request | At or under the budget in §5.2 |

**Method.** Measure, find the bottleneck, fix, measure again.

1. `python -m scripts.seed --scale 100000` fills the database with 100,000 bookings (§3.6).
2. `EXPLAIN (ANALYZE, BUFFERS)` runs for the seven hot queries: booking list, centre list by city, centre detail, duplicate guard, active-booking count, live-payment lookup, webhook gate. On the seeded data every one of them uses an index and finishes in under 0.2 ms ([perf.md](perf.md)). No hot path reads `bookings`, `payments` or `webhook_events` with a sequential scan. The catalog tables hold a few dozen rows, and a sequential scan is the cheapest plan for them.
3. `python -m scripts.perf_report latency` records p50, p95 and p99 for endpoints 5, 7, 16, 17 and 20. The `explain` command runs step 2 and exits with 1 on a sequential scan of a big table.
4. The results go to `docs/perf.md`. They are the median of three complete runs, each on a freshly seeded database. Every hot query ran in under 0.2 ms on its index. Every endpoint had a p95 under 100 ms at 100,000 bookings on a shared laptop.

**Built-in protections.**

| Topic | Design |
|---|---|
| N+1 queries | `lazy="raise_on_sql"` on every relationship. Explicit `joinedload` or `selectinload` in repositories. The query-count fixture fails a test that exceeds its budget. |
| Session | `expire_on_commit=False`, so reads after a commit run no hidden query. |
| Serialisation | Return types and `response_model`. Pydantic serialises in Rust. `ORJSONResponse` and `UJSONResponse` are deprecated in FastAPI and are not used. |
| Connection pool | `pool_size=10`, `max_overflow=10`, `pool_timeout=10 s`, `pool_pre_ping=True`, `pool_recycle=1800`. Rule: `workers × (pool_size + max_overflow)` stays below `max_connections` minus headroom. `pool_recycle` and `pool_pre_ping` act only when a connection is checked out. Behind PgBouncer in transaction mode, set `prepare_threshold=None`. |
| Thread pool | The default limit is 40 threads. Extra requests wait for a connection for up to 10 seconds. Then they receive `503`. |
| Hashing | The semaphore keeps at most `HASH_MAX_CONCURRENCY` hashes in flight, so login traffic cannot use every thread (§7.2). |
| Lists | `limit` at most 100. `offset` at most 10,000. Fixed order with an `id` tie-breaker. |
| Caching | None in the core. A measured need triggers P2 caching. |

### 8.2 Observability

Five questions guide the signals.

| # | On-call question | Signal |
|---|---|---|
| Q1 | Do patients complete bookings? | `booking.created` (with the booking, user, centre and test IDs) and `booking.status_changed` (with `from_status`, `to_status` and a `cause` of `cancel`, `payment` or `webhook`) events. A service logs each one after the commit, and a repeated cancel, a replay and a pending payment log nothing. |
| Q2 | Why do payments fail? | `payment.settled` events with `status`, `failure_reason` and `source` (`api` or `webhook`). |
| Q3 | Is the webhook healthy? | `webhook.processed`, `webhook.duplicate`, `webhook.rejected` (with `detail`) and `webhook.needs_review` (`ERROR`) and `webhook.invalid` (`WARNING`, with the failing field names). |
| Q4 | Do payments stay stuck? | SQL: `SELECT id FROM payments WHERE status = 'PENDING' AND created_at < now() - interval '15 minutes'`. A P2 job alerts on it. |
| Q5 | Is the API slow or failing? | An `http.request` event per request: `method`, `route` (the template), `status`, `duration_ms`. |

| Topic | Design |
|---|---|
| Format | JSON on standard output through `structlog`. Fields: `timestamp` (UTC), `level`, `event`, `request_id`, `user_id` (when known), domain IDs, `duration_ms`. Console format in development. |
| Request ID | `RequestIdMiddleware` accepts `X-Request-ID` when it matches `^[A-Za-z0-9-]{8,64}$`. Otherwise it generates a UUID. It stores the ID in `scope["state"]` and returns it in the response header and in every problem body. The catch-all `500` response bypasses the middleware, so its handler copies the ID into the `X-Request-ID` header itself. The middleware and the `500` handler do not use a contextvar. A middleware clears its binding before the `500` handler runs, and a sync dependency binds inside a pool thread. The user ID goes into `scope["state"]` the same way. A business event that a service logs in a pool thread still needs the request ID. An async dependency, `bind_request_id`, binds it into the log context. It runs on the event loop in the context of the request. The thread of a sync handler starts from a copy of that context. |
| Levels | `info` for business events. `warning` for a handled degradation (duplicate, throttle hit, fail-open cache). `error` for a broken invariant (`NEEDS_REVIEW`, unhandled exception). A `REJECTED` webhook event logs at `error`, except `unknown_payment`, which logs at `warning`, because a delivery can race ahead of a commit. |
| Redaction | A processor removes keys that match `password`, `token`, `secret`, `authorization` and `signature`. The code never logs raw request bodies. It never logs an email, a phone number or a name. Log lines carry IDs. JSON output also escapes line breaks, which blocks log injection. |
| Exceptions | The exception renderer runs with `show_locals=False`, because the default renderer prints local variables, a password among them. SQLAlchemy runs with `hide_parameters=True`. A processor walks `__cause__` and `__context__`. It replaces every database error with its class, SQLSTATE and constraint name. PostgreSQL error text echoes row values such as `Key (email)=(a@x.in)`. Uvicorn has its own loggers. A real `fastapi run` process printed the query string in its access log and printed the `DETAIL` line of an unhandled `IntegrityError` in the `uvicorn.error` traceback. `configure_logging()` therefore disables `uvicorn.access` and drops the traceback of `uvicorn.error` records. It must run in the lifespan start-up, because Uvicorn applies its logging configuration after the module import. `# ponytail: the messages of other unhandled exceptions are logged as they are → scrub every non-AppError message if such an exception can carry personal data`. |
| Metrics (P2) | Rate, errors and duration per route through a Prometheus instrumentator. Labels use route templates and status classes. IDs never appear as labels. |
| Health | `/health/live` (process is up) and `/health/ready` (database answers). Compose and Docker use them. The live handler is `async def`, so it answers even when every pool thread is busy. |
| Access log | `fastapi run` has no option to turn off Uvicorn's access log, so `configure_logging()` disables the `uvicorn.access` logger. The `http.request` event replaces it. It logs the method, the route template, the status and the duration, and never the query string. The middleware reads the status from the response start message. It logs `500` when the app raises, because the `500` handler's response never reaches it. |
| Verification | A test captures the logs and asserts that each event appears and that no secret, email or row value appears (AB28). |

### 8.3 Configuration

One `Settings` class (`pydantic-settings`) reads the environment. It is validated at start-up. The application does not start when a value is invalid. The class uses `extra="ignore"`, because compose shares one environment with the database container. `ENVIRONMENT` has **no default**, so a typo in its name stops the start-up instead of falling back to development defaults. A migration reads only `DATABASE_URL` (a small `DatabaseSettings` base class), so it does not need the application secrets.

Compose names the variables that the containers need. It gives each a development default through `${VAR:-default}`, so `docker compose up --build` works without a `.env` file. An optional `.env` file also reaches the containers (`env_file` with `required: false`), so every variable in the table works in Docker. The defaults are development-only values. The start-up check rejects them when `ENVIRONMENT=production`. Compose also defaults `ENVIRONMENT` to `development`, so a typo in `.env` falls back to it. This compose file is for local runs. A production deployment sets `ENVIRONMENT` itself and has no default. `Settings` reads `.env` from the working directory when it exists (`env_file=".env"`), and the process environment wins over the file.

| Variable | Default | Notes |
|---|---|---|
| `ENVIRONMENT` | none (required) | `development`, `test` or `production`. Compose sets `development`. |
| `DATABASE_URL` | none | `postgresql+psycopg://user:pass@host:5432/db`. |
| `JWT_SECRET` | none | At least 32 bytes. Placeholders are rejected outside `development`. |
| `ACCESS_TOKEN_MINUTES` | `30` | |
| `JWT_ISSUER` | `eve-healthcare-auth` | |
| `JWT_AUDIENCE` | `eve-healthcare-api` | |
| `PASSWORD_MIN_LENGTH` | `15` | 15 to 128 characters (A19). |
| `HASH_MAX_CONCURRENCY` | `4` | Number of hashing slots (§7.2). |
| `LOGIN_MAX_FAILURES` | `10` | Failed logins allowed per account inside the window. |
| `LOGIN_FAILURE_WINDOW_MINUTES` | `15` | Length of that window. |
| `AUTH_IP_LIMIT_PER_MINUTE` | `30` in production, `300` in development and test | Across signup, login and token. Host-local Docker clients share one bridge address. |
| `WEBHOOK_SECRET` | none | At least 32 bytes. |
| `WEBHOOK_SIGNATURE_REQUIRED` | `true` | Production refuses `false` (ADR-06). |
| `WEBHOOK_TOLERANCE_SECONDS` | `300` | |
| `PAYMENT_SUCCESS_RATE` | `0.8` | Simulator probability of `SUCCESS`. |
| `PAYMENT_ALLOW_SIMULATE_OVERRIDE` | `true` in `development` and `test`, `false` in `production` | Production refuses `true`. |
| `BOOKING_MIN_LEAD_MINUTES` | `30` | |
| `BOOKING_MAX_ADVANCE_DAYS` | `365` | |
| `BOOKING_MAX_ACTIVE_PER_USER` | `20` | |
| `REQUEST_BODY_LIMIT_BYTES` | `65536` | |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT` | `10`, `10`, `10` | |
| `DB_STATEMENT_TIMEOUT_MS`, `DB_LOCK_TIMEOUT_MS` | `5000`, `2000` | Two lock timeouts stay inside the 5-second webhook budget (§6.4). |
| `LOG_LEVEL`, `LOG_FORMAT` | `INFO`, `json` | |
| `DOCS_ENABLED` | `true` in `development` and `test`, `false` in `production` | |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1,::1` (Uvicorn's default) | The reverse proxy address or network. Production refuses `*` (§7.2). Uvicorn and `Settings` both read the variable. |
| `CORS_ORIGINS` | empty | Comma-separated allow-list. The field is `NoDecode` with a splitting validator. |
| `REDIS_URL` | empty | Empty disables Redis (P2). |
| `RATE_LIMIT_ENABLED` | `false` | P2. Other rate limits (§8.4). |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | none | Used by the seed script only. Compose supplies `admin@example.com` and a development password. `ADMIN_EMAIL` is a plain string. `EmailStr` accepts `example.com` and rejects `.local` and `.test`. In production the seed refuses a placeholder or a password shorter than `PASSWORD_MIN_LENGTH`. |
| `TEST_DATABASE_URL` | `postgresql+psycopg://eve:eve-dev-password@localhost:5432/eve_test` | Tests only. This is the compose database user, which can create `eve_test`. With your own PostgreSQL, use a role that has `CREATEDB`. The `test` compose service overrides it with the host `db`. |
| `SEED_ON_START` | `true` in compose | Read by the compose `migrate` service. The API ignores it. |

The start-up check stops the application in these cases. `ENVIRONMENT` is missing. A secret is missing or shorter than 32 bytes. A secret is a placeholder and `ENVIRONMENT` is not `development`. A value is a placeholder when it starts with `dev-only-`, `whsec_demo_only_` or `change-me`, which covers the compose defaults and the example secret in §6.5. In production, `WEBHOOK_SIGNATURE_REQUIRED` is `false` or `PAYMENT_ALLOW_SIMULATE_OVERRIDE` is `true`. `DOCS_ENABLED` defaults to `false` in production, and an operator can turn it on. A test covers each rule.

### 8.4 Rate limiting

The login throttle (P1) is in §7.2. The other limits are P2.

| Endpoint | Limit | Key |
|---|---|---|
| `POST /auth/login`, `/auth/token` | `LOGIN_MAX_FAILURES` failures per `LOGIN_FAILURE_WINDOW_MINUTES` | Account, by the hashed email (P1) |
| `POST /auth/signup`, `/auth/login`, `/auth/token` | `AUTH_IP_LIMIT_PER_MINUTE` | Client IP (P1) |
| `POST /bookings/` | 20 per minute | User (P2) |
| `POST /payments/` | 10 per minute | User (P2) |
| Everything else | 120 per minute | Client IP (P2) |
| `POST /payments/webhook/` | Exempt. The signature and the body cap protect it. | — |

Storage is in memory without Redis and Redis with `REDIS_URL`. An exceeded limit returns `429 RATE_LIMITED` with `Retry-After` in the problem format. The limiter is the `limits` library behind an `async def` dependency (`auth_gate`) of about 25 lines. It is not `slowapi`. The client IP comes from the proxy headers. Set `FORWARDED_ALLOW_IPS` to the proxy address or network, and never to `*` (production refuses it), because `*` lets a client choose its own IP. In-memory counters are per process and only grow, so use Redis when you run more than one worker or expose the service publicly.

### 8.5 Caching (P2)

Cache-aside on `GET /centres/`, `GET /centres/{id}` and `GET /tests/`. The TTL is 60 seconds. A generation counter (`catalog:gen`) is part of every key. Any catalog write increments it. Invalidation costs one `INCR` and needs no key scan. The cache holds only public catalog data, never per-user data. When Redis fails, the code reads the database and logs a warning. Tests run against a real Redis container. The trigger to enable it is a measured problem, not a guess.

---

## 9. Testing strategy

### 9.1 Shape

| Level | Scope | Share | Speed |
|---|---|---|---|
| Unit | Pure functions: `settle`, `state`, `policy`, `signature`, `security`, throttle, pagination, error catalogue, settings. No database. | about 35% | Milliseconds |
| Integration | HTTP through the real app against real PostgreSQL. | about 60% | Tens of milliseconds |
| Concurrency | Threads against the real app. Races that need real commits. | about 5% | Under a second each |

Rules: real PostgreSQL only (never SQLite, because locks, partial indexes and `ON CONFLICT` differ). Tests assert state and outcomes, not calls. Each test reads as one specification (`test_<unit>_<scenario>_<result>`) and follows Arrange, Act, Assert. Two test doubles are allowed: a fixed clock and a chosen simulator result.

### 9.2 Infrastructure

| Piece | Design |
|---|---|
| Database | `TEST_DATABASE_URL` (default: the compose user on `localhost:5432/eve_test`, and the host `db` inside the `test` compose service). The session fixture creates the database if missing and runs `alembic upgrade head` once. This proves the migrations. The fixture disposes every engine. With `filterwarnings = ["error"]`, one unclosed connection fails the run, and the skeleton test showed it. |
| Isolation | After each test, one `TRUNCATE … RESTART IDENTITY CASCADE` over the tables in `Base.metadata.sorted_tables` (not `alembic_version`). The fixture sets `lock_timeout`, because `TRUNCATE` takes an exclusive lock. It costs 3 to 4 ms for the seven tables. Real commits happen, so concurrency tests work. SAVEPOINT isolation is faster, but other threads cannot see uncommitted rows, so races become untestable. If a suite ever adopts it, the mode must be `create_savepoint`, because the default lets `session.rollback()` end the outer transaction. |
| App | `create_app(settings)` once per session. Tests use `with TestClient(app, raise_server_exceptions=False)`, so the lifespan runs and the `500` handler can be tested. `dependency_overrides` replace settings, clock and simulator. |
| Factories | Plain functions in `tests/factories.py` (`make_user`, `make_centre_test`, `make_booking`). They insert through SQLAlchemy. |
| Auth helper | `auth_headers(user)` builds a token with `create_access_token`. Expiry tests mint a token with a past `exp`. |
| Query counter | A fixture counts statements through the SQLAlchemy `before_cursor_execute` event and asserts the budget. It skips the authentication lookup. |
| Index check | `indexes_used(engine, action)` runs an action, explains each statement that it ran with sequential scans off, and returns the index names. A test asserts the index that §3.4 names for each payment query. |
| Concurrency | `ThreadPoolExecutor` and a `threading.Barrier`, so all requests start together. One client per thread. The CI job runs the concurrency group 20 times. |
| Time | The injected `Clock` gives a fixed `now`. Window and appointment tests move the clock. |
| Logs | A fixture captures structlog output for the log tests (§8.2). |
| Throttle | Tests build a fresh in-memory throttle for each test, so counters never leak between tests. |

### 9.3 Requirements to tests

| Group | Cases (each a named test) | Covers |
|---|---|---|
| Auth (AB6, AB19, AB21, AB22, AB25, AB35) | Signup: happy path, duplicate, duplicate in other case, 14-character password, leading spaces kept, extra `role`, no hash in response. Signup edge cases: `NUL` and control characters, concurrent double signup, throttle. Login on `/auth/login` and `/auth/token`: happy path, wrong password, unknown email, the seeded admin (`admin@example.com`) on both endpoints, token claims. Token failures: expired, future `iat`, `alg: none`, wrong secret. Throttle and limits: account throttle, case variants share one counter, IP throttle, a spoofed `X-Forwarded-For` does not change the key. A 10,000-character email returns `422`, and a 100,000-character email returns `413`. Every hashing slot taken returns `503` at once. `GET /users/me`. | PDF §1, §4 |
| Catalog (AB2, AB17, AB18, AB33) | Public read. Admin create and update. Patient forbidden. Duplicate. A centre without a city. Price validation. A lower-case or long currency code gives `422`. Price upsert is idempotent, sets `updated_at`, and returns `201` on create and `200` on update. Unknown centre or test on `PUT`. Soft `DELETE` and restore by `PATCH`. Repeated `DELETE` returns `204`. Inactive rows hidden. Filters by city, test and text. `%` and `_` in `q` match literally. Price shown when the list filters by test. Empty `PATCH` and `PATCH` with `null` return `422`. Pagination bounds. Detail page query count. | PDF §2 |
| Bookings (AB1, AB3, AB7, AB27, AB29) | The cases on the card in §5.4, plus list, get, cancel and ownership. `Location` is a path and ignores a hostile `Host` header. An appointment of `9999-12-31T23:59:59-14:00` returns `422` without an error. | PDF §3, §4 |
| Payments (AB4, AB5, AB20, AB30, AB31) | The cases on the card in §5.4, plus a cancelled booking that keeps a `SUCCESS` payment (replay `200`). | PDF §2, §4 |
| Webhook (AB8 to AB13, AB16, AB23, AB24, AB32) | Valid `SUCCESS`. Valid `FAILED`. Sequential duplicate. 20 concurrent duplicates. Different event IDs with one result. Different IDs with different references. Conflicting terminal result. Same reference on a second payment, also at the same moment (the retry path). A payment settled without a reference stores the first one. Unknown extra fields. Signed body with an invalid field returns `422` and stores nothing. `NUL` in each text field. Unknown payment (stored, redelivery is a duplicate). Missing signature. Wrong signature. Stale timestamp. Changed body. Signature not required outside production. Amount mismatch. Currency mismatch. Oversized body, with `Content-Length` and chunked. Malformed JSON. Settles a `PENDING` payment. Failure rolls back and leaves no event row. A lock timeout returns `503` and is not retried. A deadlock is retried. The documented signature example. Simulator script with retries. | PDF §3, §4 |
| Authorization | AB1 to AB6, AB14, AB15, AB20 and AB27 from §7.4, run against every protected route. | PDF §4 |
| State machine | Every booking transition (a 4 × 4 grid). Every row of the `settle()` table. | PDF §3, §2 |
| Security units | Hash and verify (NFC, no stripping). The hashing semaphore never runs more slots than it has and turns surplus callers away at once. Token encode and decode, with the real clock and the key-length option. Signature sign and verify. Throttle key and limits. The dummy hash costs as much as a real one. | PDF §1, §3 |
| Configuration | Production refuses each unsafe setting (AB26), a placeholder secret (including the §6.5 example) and a short secret. `ENVIRONMENT` is required. `CORS_ORIGINS` parses a comma list. `.env` is read when it exists. | Bonus |
| Logging | Each event in §8.2 appears, with the request ID. No secret, email, query string or row value appears in the structlog output. The same holds for the standard output and standard error of a real `fastapi run` process (AB28). A constraint error logs only its name. The catch-all `500` body and its `X-Request-ID` header carry the request ID, and the response has `Cache-Control: no-store`. The `http.request` event for a `500` logs status `500`. | Bonus |
| OpenAPI and HTTP | Every operation has a summary, a unique `operationId` and its error responses. The route inventory matches §5.2. `HEAD` works on every `GET` route (AB34). `Cache-Control: no-store` is on every response, `404`, `405`, `422` and the catch-all `500` included. A `405` lists every method of the path in `Allow`. No response redirects. A request that names both `Content-Length` and `Transfer-Encoding` gets `400` (AB36). | Bonus |
| Database | Migrations run from empty. `alembic check` is clean. The schema from the migrations equals the schema from the models, constraints and indexes included. Every foreign key has an index (query in §3.6). The schema has no native enum type. Each rule in §3.5 rejects a bad row and accepts a good one. Seed runs twice without error. | PDF §2, Bonus |

The suite holds about 1,370 tests. The grids (every booking transition, every row of `settle()`, every rejected input) run each case as its own test, so the count is higher than the number of behaviours.

### 9.4 Tooling and gates

- `pytest --cov=app --cov-report=term-missing --cov-fail-under=85`. The 85% overall gate fails the build. Each checkpoint also reports the six logic modules (`service` modules, `state`, `policy`, `settlement`, `signature`, `security`). The target for them is 90%.
- Ruff rule families: `E, F, W, I, N, UP, B, A, C4, C90, SIM, PL, RUF, S, PT, TID, DTZ, T20, PERF, FAST`. `DTZ` blocks naive datetimes. `T20` blocks `print`. `FAST` adds FastAPI checks.
- `mypy --strict` with the Pydantic plugin is the type gate. `ty` is still a 0.x beta, so it stays optional.
- `pip-audit` scans the locked dependencies. It is necessary but not sufficient, because advisory databases lag.
- Starlette 1.7's `TestClient` warns that `httpx` is deprecated in favour of `httpx2`. The dev group installs `httpx2`, so `filterwarnings = ["error"]` needs no ignore. A class whose name starts with `Test` triggers a collection warning in every test module that imports it. `filterwarnings = ["error"]` turns that warning into an error. This is why the error class is `DiagnosticTestNotFound` and not `TestNotFound`.
- `pre-commit` runs Ruff and file checks before each commit.

---

## 10. Delivery

### 10.1 Docker and compose

**Image.** Multi-stage, with three stages. The `builder` stage runs `uv sync --frozen --no-dev` from a pinned `uv` image. The `test` stage builds on it, adds the dev group and `tests/`, and runs `pytest`. Only the `test` compose service uses it. The last stage is the runtime image, so it is the default target. It copies the virtual environment, `app/`, `scripts/`, `migrations/`, `alembic.ini` and `pyproject.toml`. The last one must stay, because `fastapi run` reads its entrypoint from `[tool.fastapi]`. The `scripts/` folder must stay, because the `migrate` service runs the seed from it. The container runs as a non-root user. It uses the exec form of `CMD`, so signals reach the process and shutdown is graceful.

```dockerfile
FROM python:3.12-slim-trixie AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
WORKDIR /srv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
COPY scripts ./scripts
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --frozen --no-dev

FROM builder AS test
ENV PATH="/srv/.venv/bin:$PATH"
RUN uv sync --frozen
COPY tests ./tests
CMD ["pytest", "-q"]

FROM python:3.12-slim-trixie
RUN useradd --uid 10001 --create-home app
WORKDIR /srv
COPY --from=builder --chown=app:app /srv /srv
ENV PATH="/srv/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER app
EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=10 CMD python -c "import urllib.request as u; u.urlopen('http://localhost:8000/health/live')"
CMD ["fastapi", "run", "--host", "0.0.0.0", "--port", "8000"]
```

**Compose file.**

```yaml
name: eve-healthcare

x-env: &env
  ENVIRONMENT: ${ENVIRONMENT:-development}
  DATABASE_URL: postgresql+psycopg://${POSTGRES_USER:-eve}:${POSTGRES_PASSWORD:-eve-dev-password}@db:5432/${POSTGRES_DB:-eve}
  JWT_SECRET: ${JWT_SECRET:-dev-only-jwt-secret-change-me-0123456789abcdef}
  WEBHOOK_SECRET: ${WEBHOOK_SECRET:-dev-only-webhook-secret-change-me-0123456789}
  ADMIN_EMAIL: ${ADMIN_EMAIL:-admin@example.com}
  ADMIN_PASSWORD: ${ADMIN_PASSWORD:-dev-only-admin-password}
  SEED_ON_START: ${SEED_ON_START:-true}

x-app: &app
  image: eve-healthcare-api:dev
  build: .
  env_file:
    - path: .env
      required: false
  environment: *env

services:
  db:
    image: postgres:17
    environment:
      POSTGRES_USER: ${POSTGRES_USER:-eve}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-eve-dev-password}
      POSTGRES_DB: ${POSTGRES_DB:-eve}
    volumes: ["pgdata:/var/lib/postgresql/data"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U $${POSTGRES_USER} -d $${POSTGRES_DB}"]
      interval: 3s
      timeout: 3s
      retries: 20

  migrate:
    <<: *app
    command: ["sh", "-c", "alembic upgrade head && if [ \"$$SEED_ON_START\" = true ]; then python -m scripts.seed; fi"]
    depends_on:
      db: {condition: service_healthy}
    restart: "no"

  api:
    <<: *app
    ports: ["${API_PORT:-8000}:8000"]
    depends_on:
      migrate: {condition: service_completed_successfully}

  test:
    <<: *app
    profiles: ["test"]
    image: eve-healthcare-api:test
    build:
      context: .
      target: test
    environment:
      <<: *env
      TEST_DATABASE_URL: postgresql+psycopg://${POSTGRES_USER:-eve}:${POSTGRES_PASSWORD:-eve-dev-password}@db:5432/eve_test
    depends_on:
      db: {condition: service_healthy}
    command: ["pytest", "-q"]

volumes:
  pgdata:
```

**Development override** (`make db` adds it, so the database port is published only for runs on the host):

```yaml
# Publishes the database port for runs on the host. Used by `make db`.
services:
  db:
    ports: ["127.0.0.1:${DB_PORT:-5432}:5432"]
```

| Service | Role |
|---|---|
| `db` | PostgreSQL 17 with a named volume at `/var/lib/postgresql/data` and a `pg_isready` health check. It publishes no port. (PostgreSQL 18 mounts `/var/lib/postgresql` instead.) |
| `migrate` | Runs `alembic upgrade head`, then the idempotent seed when `SEED_ON_START=true` (the compose default). Exits when done. |
| `api` | Starts after `migrate` completes successfully. Publishes port 8000. |
| `test` | Behind the profile `test`. Builds the `test` stage and runs `pytest` against the `db` service. The fixture creates `eve_test`, because the compose user is a superuser. |
| `redis` | Optional (P2). Image `redis:8`. Behind the compose profile `cache`. |

Six details matter:

- **No `.env` file is needed.** Compose names the variables that the containers need and gives each a development default through `${VAR:-default}`, so `docker compose up --build` works on a clean checkout. An optional `.env` file reaches the containers as well (`env_file` with `required: false`). A value such as `PAYMENT_SUCCESS_RATE` or `WEBHOOK_SIGNATURE_REQUIRED` then works in Docker. The names in `environment` win over `.env`, so a `DATABASE_URL` for host runs cannot break the containers. The start-up check rejects the placeholder secrets when `ENVIRONMENT=production`. The README says this in one line.
- **The database port is not published by default.** `docker compose up` then cannot fail on a busy port 5432. `API_PORT` moves the API port when 8000 is busy. `make db` adds the override file and publishes `127.0.0.1:${DB_PORT:-5432}` for the host-side path.
- **Health check over TCP.** The official PostgreSQL image starts a temporary server that listens only on the Unix socket while it initialises. `pg_isready -h 127.0.0.1` succeeds only after the real server is up.
- **Tests in Docker.** `docker compose --profile test run --rm test` needs neither `uv` nor a host Python.
- **Reset.** `docker compose down -v` deletes the volumes of the project that Compose finds. Run it from the project folder, or name the project and the file (`-p eve-healthcare -f docker-compose.yml`). A wrong folder means a wrong project.
- **Build context.** `.dockerignore` lists `.git`, `.venv*/`, caches, `.env`, `docs/` and the assignment PDF. The project folder also holds virtual environments and caches, many megabytes together. Without the file, Docker sends all of it to the daemon on every build.

The stack is checked from a clean checkout. `docker compose up --build --wait` is healthy in about 20 seconds on an idle machine with no `.env` file (21 seconds in the last run). `migrate` finishes before `api` starts, and the seeded admin is in the database. Values from `.env` reach the container, and a `DATABASE_URL` in `.env` does not override the compose value. The `test` profile runs the whole suite in the container. The container runs as UID 10001. The `db` service publishes no port, and with the override it publishes the port that `DB_PORT` names on `127.0.0.1`.

### 10.2 Continuous integration

GitHub Actions run on every push and pull request to `main`. Jobs run in parallel.

| Job | Steps |
|---|---|
| `quality` | `uv sync --frozen`, `ruff check`, `ruff format --check`, `mypy app` |
| `test` | PostgreSQL 17 service. `alembic upgrade head`. `alembic check`. `pytest` with the coverage gate. Then the concurrency group, 20 times in a row. |
| `audit` | `pip-audit` on the locked dependencies |
| `docker` | Build the image. `docker compose up --build --wait` with no `.env` file. `curl /health/ready`. Log in as the seeded admin and check that the sample catalog has 5 centres. Then `docker compose --profile test run --rm test`. |
| `compat` (P2) | Run the `test` job again on Python 3.14 to catch forward-compatibility problems early. |

The workflow sets `permissions: contents: read` and cancels superseded runs. A red pipeline blocks the merge. Each action is pinned to a major version.

### 10.3 Git workflow

- Trunk-based. `main` always passes CI. Branches are short (`feat/…`, `fix/…`, `chore/…`).
- One vertical slice per commit or pull request. Aim for about 100 lines, and never above 300 without a reason.
- Conventional Commits. The body explains why. Example: `feat(payments): settle payments through one idempotent function`.
- Commit `uv.lock` and the generated `requirements.txt` and `requirements-dev.txt`. Never commit `.env`, `.venv` or caches.
- `.gitignore` lists `.env`, `.venv*/`, caches and the assignment PDF.
- Every commit passes lint, types and tests.
- Tag `v0.1.0` at submission. The tag matches `version` in `pyproject.toml`.


## 11. Design decisions

Each record is short. The public `docs/ARCHITECTURE.md` keeps this table. A file per record in `docs/decisions/` is an option after approval, and the plan reserves no task or hours for it.

| ID | Decision | Status | Rejected alternatives | Consequence |
|---|---|---|---|---|
| ADR-01 | **Modular monolith.** Feature packages `auth`, `catalog`, `bookings`, `payments`. Shared `models/` folder. | Accepted | Layer-first folders. Microservices. | One deployable unit. A feature change stays in one package. |
| ADR-02 | **Sync SQLAlchemy 2.0 with psycopg 3 and plain `def` handlers**. | Accepted | Async SQLAlchemy with asyncpg. | FastAPI runs `def` handlers in a thread pool, so blocking calls do not stall the event loop. Argon2 needs no special handling. Tests need no event loop. Moving to async later touches every service and repository method. |
| ADR-03 | **SQLAlchemy over SQLModel**. Concrete classes, no repository interfaces. | Accepted | SQLModel. Abstract ports with in-memory fakes. | Full access to composite foreign keys, partial indexes, `ON CONFLICT` and row locks. Tests use real PostgreSQL. |
| ADR-04 | **Service owns the transaction. The booking is the aggregate root.** Lock the booking row first, with `FOR NO KEY UPDATE`, `populate_existing` and the owner predicate. | Accepted | Commit in the session dependency. Payment rows first. Plain `FOR UPDATE`. Lock first and check the owner after. | One commit per use case. One lock order. No deadlock between pay, webhook and cancel. A stranger never waits on the owner's lock. |
| ADR-05 | **One-transaction payment with an in-process simulator. One `settle()` function for both entry points**. | Accepted | Two-phase payment now. Separate logic per endpoint. | No stuck `PENDING` payment. No lock spans a network call, because the simulator does no I/O. Two-phase is the documented upgrade for a real gateway (§6.6). |
| ADR-06 | **Webhook: HMAC signature on the raw body, required by default**. A flat contract that ignores unknown fields. A transactional inbox that records every signed event that has a valid body. Four idempotency layers. | Accepted | Check-then-insert. A signature-free webhook. An asynchronous inbox worker. A nested event envelope with types. `4xx` for business failures. | Exactly-once effect under at-least-once delivery. Duplicates are cheap. Business failures get `200`, so the provider does not retry them. |
| ADR-07 | **Wrong owner returns `403`**. | Accepted | `404` to hide existence. | Clear contract. Random UUIDs prevent enumeration. UUIDv7 keeps 74 random bits. One helper flips it. |
| ADR-08 | **Login: JSON `POST /auth/login` plus the OAuth2 form `POST /auth/token`**. | Accepted | Form only. JSON only. | Swagger *Authorize* works and JSON clients work. One service method sits behind both. |
| ADR-09 | **A failed payment ends the booking as `FAILED`**. | Accepted | Keep `PENDING` for retry. | Matches the PDF state names. The user books again. The alternative is one change in `settle()` (§4.2). |
| ADR-10 | **Errors use RFC 9457 problem details with a stable `code`**. | Accepted | A custom `{error: {...}}` envelope. | A standard format. One handler module. |
| ADR-11 | **Data types:** UUIDv4 keys, `numeric(10,2)` money, `varchar(32)` plus a named `CHECK` for statuses, `timestamptz` in UTC. | Accepted | `bigint` identity keys, native enums, floats. | Exact money. Simple status changes. UUIDv4 fragments indexes past about 10 million rows. |
| ADR-12 | **`limit`/`offset` pagination with caps.** | Accepted | Keyset pagination. | Familiar to clients. Indexes are ready for keyset. |
| ADR-13 | **Argon2id (`pwdlib`) with NFC-normalised, never-stripped passwords. Non-blocking hashing slots taken in an async dependency. HS256 JWT (PyJWT) with the real clock, access token only. Auth throttle per account (hashed key) and per IP. Minimum length 15**. | Accepted | bcrypt. RS256. Refresh tokens. Composition rules. A semaphore that waits inside a sync handler. | Current FastAPI guidance and NIST. No revocation in v1. A burst of logins gets `503` at once and never starves the thread pool. |
| ADR-14 | **No Celery. Redis optional (P2)**. | Accepted | Celery worker for webhooks. Mandatory Redis. | Fewer containers. Deterministic state. The inbox makes a worker additive. |
| ADR-15 | **Tooling:** `uv`, Ruff, `mypy --strict`, pytest on real PostgreSQL, GitHub Actions. | Accepted | pip and requirements files as the source. SQLite tests. | Reproducible builds. Realistic tests. A generated `requirements.txt` serves readers without `uv`. |
| ADR-16 | **Runtime:** Python 3.12 or newer, SQLAlchemy 2.0.54 (below 2.1), PostgreSQL 17, `fastapi run` in a non-root container. | Accepted | Python 3.14. SQLAlchemy 2.1. PostgreSQL 18. | Matches the development machine. Avoids a release that is days old. |
| ADR-17 | **Paths follow the PDF** (`/payments/`, `/payments/webhook/`). No version prefix. Every route answers both slash spellings, and `HEAD` wherever `GET` works, without a redirect. | Accepted | `/api/v1/...`. Default slash redirects. | Matches the PDF exactly. Webhook senders that treat a redirect as a failure work. RFC 9110 is met. A version prefix costs one line later. |
| ADR-18 | **Simulator with a success rate, an override outside production and an asynchronous `PENDING` mode.** | Accepted | Random result only. | The webhook becomes demonstrable and testable end to end. |
| ADR-19 | **Catalog rows are deactivated, never deleted** (A14). | Accepted | Hard delete with cascade. | Booking history stays valid. `DELETE` returns `204` and `PATCH` restores. |
| ADR-20 | **Scope: production quality over the assignment's 3 to 4 hour estimate.** All six sections, the endpoints that the design needs, tests for every one, and documentation. | Accepted | A minimal scope with fewer tests and no hardening. | More work, and a service whose behaviour under repeats and races is tested. |
| ADR-21 | **Auth gate: one `async def` dependency checks the throttle and takes a hashing slot without waiting, before the request needs a pool thread.** | Accepted | A blocking semaphore in the handler. A middleware for the throttle. | The thread pool stays free during a login burst (0.02 s against 1.8 s for `/health/live` in the check). The dependency sees the parsed body, so it can key on the email. |
| ADR-22 | **Request context lives in `scope["state"]`. Response headers that every response needs (`no-store`, request ID) come from pure ASGI middleware. The catch-all `500` handler sets the same headers itself.** | Accepted | Contextvars. Headers set in dependencies. | The catch-all `500` handler and every error response see the same values. |
