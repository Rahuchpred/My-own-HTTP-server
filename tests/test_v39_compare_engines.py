"""V39 tests for engine comparison tooling and gate calculation."""

from tools.compare_engines import _gate_results, _markdown_table, _run_profile


def test_gate_results_reports_expected_keys() -> None:
    gates = _gate_results(
        threadpool={
            "rps": 100.0,
            "error_rate": 0.0,
            "p95_ms": 300.0,
            "requests": 100,
            "errors": 0,
            "p50_ms": 1.0,
            "status_counts": {},
        },
        selectors={
            "rps": 220.0,
            "error_rate": 0.0,
            "p95_ms": 100.0,
            "requests": 220,
            "errors": 0,
            "p50_ms": 1.0,
            "status_counts": {},
        },
    )

    assert "checks" in gates
    assert "selectors_vs_threadpool_rps_ratio" in gates
    assert isinstance(gates["all_passed"], bool)


def test_markdown_table_contains_both_engines() -> None:
    table = _markdown_table(
        {
            "threadpool": {
                "requests": 100,
                "errors": 1,
                "error_rate": 0.01,
                "rps": 50.0,
                "p50_ms": 2.0,
                "p95_ms": 8.0,
            },
            "selectors": {
                "requests": 200,
                "errors": 0,
                "error_rate": 0.0,
                "rps": 120.0,
                "p50_ms": 1.0,
                "p95_ms": 4.0,
            },
        }
    )

    assert "| threadpool |" in table
    assert "| selectors |" in table


def test_run_profile_returns_summary_shape() -> None:
    summary = _run_profile(
        engine="threadpool",
        path="/",
        concurrency=2,
        duration=0.3,
        timeout=2.0,
        keepalive=False,
        pipeline_depth=1,
    )

    assert set(summary.keys()) == {
        "requests",
        "errors",
        "error_rate",
        "rps",
        "p50_ms",
        "p95_ms",
        "status_counts",
    }
    assert summary["requests"] > 0
