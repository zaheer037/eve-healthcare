# Performance

This page reports what I measured at 100,000 bookings, with one Uvicorn worker. Anyone can repeat
it with the two commands at the end.

## Result

Every endpoint answers with a p95 under 100 ms at 100,000 bookings. Every hot query runs in under
0.2 ms and uses the index that the design names. No query reads `bookings`, `payments` or
`webhook_events` with a sequential scan.

## The data

`python -m scripts.seed --scale 100000` builds the data set in about 17 seconds.

| Table | Rows | Size with indexes |
|---|---:|---:|
| `users` | 10,001 (one admin and 10,000 load users) | |
| `bookings` | 100,000 (60% `CONFIRMED`, 20% `PENDING`, 10% `FAILED`, 10% `CANCELLED`) | 34 MB |
| `payments` | 90,000 (a settled payment for every confirmed, failed and cancelled booking, and a pending one for half of the pending bookings) | 31 MB |
| Database | | 76 MB |

Each user holds about ten bookings. Appointments spread over the 365-day booking window. The seed
runs `ANALYZE` at the end, so the planner has fresh statistics.

## The seven hot queries

`EXPLAIN (ANALYZE, BUFFERS)` of each query, as the API sends it. A list or a detail page is two
statements (the count and the page, or the centre and its tests), so it has two rows.

| Query | Execution ms | Planning ms | Buffers hit / read | Indexes |
|---|---:|---:|---:|---|
| booking list (1 of 2) | 0.02 | 0.03 | 18 / 0 | `ix_bookings_user_created` |
| booking list (2 of 2) | 0.08 | 0.16 | 29 / 0 | `ix_bookings_user_created`, `pk_centres` |
| centre list by city (1 of 2) | 0.01 | 0.03 | 2 / 0 | `ix_centres_city_active` |
| centre list by city (2 of 2) | 0.02 | 0.03 | 2 / 0 | `ix_centres_city_active` |
| centre detail (1 of 2) | 0.01 | 0.02 | 2 / 0 | `pk_centres` |
| centre detail (2 of 2) | 0.03 | 0.06 | 3 / 0 | `pk_centre_tests` |
| duplicate guard | 0.01 | 0.06 | 3 / 0 | `uq_bookings_active_slot` |
| active-booking count | 0.03 | 0.05 | 14 / 0 | `uq_bookings_active_slot` |
| live-payment lookup | 0.01 | 0.04 | 4 / 0 | `uq_payments_live_per_booking` |
| webhook gate | 0.05 | 0.01 | 11 / 0 | `uq_webhook_events_provider_event` |

The duplicate guard is a unique index, so an insert that repeats a slot fails there. The table
shows the equivalent probe with the same columns and the same predicate. Every page came from the
shared buffers (`read` is 0), because the data set fits in memory.

The catalog tables hold a few dozen rows. The planner reads them with a sequential scan when that
is cheaper, and `centres` and `centre_tests` show no index in the plans where it did.

## Endpoint latency

1,000 requests per endpoint from 8 client threads, against one Uvicorn worker on the same machine.
Each row is the median of three complete runs, and each run started from a freshly seeded
database, because a second run would meet the cap of 20 active bookings per user. The last column
gives the range of the p95 across the three runs.

| Endpoint | Requests | Errors | p50 ms | p95 ms | p99 ms | Range of p95 ms |
|---|---:|---:|---:|---:|---:|---|
| `GET /centres/?city=` | 1,000 | 0 | 21.2 | 33.8 | 57.2 | 25.7 to 41.4 |
| `GET /centres/{id}` | 1,000 | 0 | 21.0 | 42.4 | 65.4 | 25.2 to 43.8 |
| `GET /bookings/` (with a token) | 1,000 | 0 | 32.8 | 57.2 | 89.8 | 38.4 to 65.7 |
| `POST /bookings/` | 1,000 | 0 | 34.1 | 47.7 | 67.1 | 45.6 to 48.6 |
| `POST /payments/` | about 50 | 0 | 36.9 | 50.4 | 58.4 | 49.0 to 64.3 |

The payment run is short because the load users hold only about one pending booking each without
a payment. Signup, login and token are not in the table. They spend about 55 to 67 ms in Argon2
on the development machine, and `HASH_MAX_CONCURRENCY` (default 4) caps how many run at once.

## Reading the numbers

- **The database is not the cost.** A statement takes 0.01 to 0.16 ms. A request takes 20 to 60 ms.
  The rest is Python: the token check, the request validation, the hand-off to a worker thread and
  the JSON response. An index cannot make that faster. More workers can.
- **The machine is shared.** I ran the tests on a laptop with 8 cores. Other containers ran
  beside it, and the load average was between 3 and 5 during the three runs. One earlier run at a
  load average of 7 showed p95 values of 91 to 148 ms for the reads, while the SQL times did not
  change. Treat the p95 column as a range and not as a promise. The median of the p50 is steadier.
- **The statement counts are tested.** A test asserts the query budget of every endpoint
  (`tests/integration/test_query_budgets.py`), so an accidental extra query fails a build long
  before it shows in a latency table.

## Known limits

- Offset pagination reads and skips the rows before the page. The cap of 10,000 on `offset`
  bounds the cost. A user with thousands of bookings needs keyset pagination.
- The `q` filter on centres uses `ILIKE` and reads the small catalog with a sequential scan. A
  catalog of many thousand centres needs a trigram index.
- There is no cache. A measured need starts the caching work.

## Repeat it

```bash
# 1. Fill a scratch database. Never run this against data that matters.
DATABASE_URL=postgresql+psycopg://eve:eve-dev-password@localhost:5432/eve_perf \
  uv run python -m scripts.seed --scale 100000

# 2. The plans of the seven hot queries. The command exits with 1 on a sequential scan of a big table.
DATABASE_URL=... uv run python -m scripts.perf_report explain

# 3. The latency of five endpoints, with the API running on the same database.
DATABASE_URL=... uv run fastapi run --port 8000 &
DATABASE_URL=... uv run python -m scripts.perf_report latency --url http://127.0.0.1:8000
```
