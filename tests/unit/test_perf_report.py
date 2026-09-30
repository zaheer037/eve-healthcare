"""The pure parts of the performance report: reading a plan and summarising latencies."""

import pytest

from scripts.perf_report import BIG_TABLES, LatencyRow, percentile, summarise_plan

PLAN_WITH_AN_INDEX = [
    {
        "Plan": {
            "Node Type": "Limit",
            "Plans": [
                {
                    "Node Type": "Nested Loop",
                    "Plans": [
                        {
                            "Node Type": "Index Scan",
                            "Index Name": "ix_bookings_user_created",
                            "Relation Name": "bookings",
                            "Shared Hit Blocks": 12,
                            "Shared Read Blocks": 3,
                        },
                        {
                            "Node Type": "Index Scan",
                            "Index Name": "pk_centres",
                            "Relation Name": "centres",
                            "Shared Hit Blocks": 5,
                            "Shared Read Blocks": 0,
                        },
                    ],
                }
            ],
            "Shared Hit Blocks": 20,
            "Shared Read Blocks": 3,
        },
        "Planning Time": 0.4,
        "Execution Time": 1.5,
    }
]


def test_a_plan_summary_lists_the_indexes_and_the_times() -> None:
    summary = summarise_plan("booking list", PLAN_WITH_AN_INDEX)
    assert summary.name == "booking list"
    assert summary.indexes == ["ix_bookings_user_created", "pk_centres"]
    assert (summary.planning_ms, summary.execution_ms) == (0.4, 1.5)
    assert (summary.buffers_hit, summary.buffers_read) == (20, 3)
    assert summary.big_table_scans == []


def test_a_sequential_scan_of_a_big_table_is_reported_and_a_small_one_is_not() -> None:
    plan = [
        {
            "Plan": {
                "Node Type": "Hash Join",
                "Plans": [
                    {"Node Type": "Seq Scan", "Relation Name": "bookings"},
                    {"Node Type": "Seq Scan", "Relation Name": "centres"},
                    {"Node Type": "Seq Scan", "Relation Name": "webhook_events"},
                ],
            },
            "Planning Time": 0.1,
            "Execution Time": 90.0,
        }
    ]
    summary = summarise_plan("bad", plan)
    assert summary.big_table_scans == ["bookings", "webhook_events"]  # centres has a few rows
    assert {"bookings", "payments", "webhook_events"} == set(BIG_TABLES)


def test_an_insert_reports_its_conflict_arbiter_index() -> None:
    plan = [
        {
            "Plan": {
                "Node Type": "ModifyTable",
                "Conflict Resolution": "NOTHING",
                "Conflict Arbiter Indexes": ["uq_webhook_events_provider_event"],
            },
            "Planning Time": 0.05,
            "Execution Time": 0.2,
        }
    ]
    assert summarise_plan("gate", plan).indexes == ["uq_webhook_events_provider_event"]


@pytest.mark.parametrize(
    ("values", "p", "expected"),
    [
        ([10.0], 95, 10.0),
        ([1.0, 2.0, 3.0, 4.0], 50, 2.0),
        ([1.0, 2.0, 3.0, 4.0], 100, 4.0),
        (list(map(float, range(1, 101))), 95, 95.0),
        (list(map(float, range(1, 101))), 99, 99.0),
        ([5.0, 1.0, 3.0], 50, 3.0),  # the input need not be sorted
    ],
)
def test_percentile_is_the_nearest_rank(values: list[float], p: int, expected: float) -> None:
    assert percentile(values, p) == expected


def test_a_latency_row_keeps_the_counts_and_the_percentiles() -> None:
    row = LatencyRow.of("GET /centres/", [10.0, 20.0, 30.0, 40.0], errors=1)
    assert (row.requests, row.errors) == (4, 1)
    assert (row.p50_ms, row.p95_ms, row.max_ms) == (20.0, 40.0, 40.0)


def test_the_percentile_of_nothing_is_refused() -> None:
    with pytest.raises(ValueError, match="no values"):
        percentile([], 50)
