"""Unit tests for scenario validation and execution engine."""

from __future__ import annotations

from response import HTTPResponse
from scenario_engine import ScenarioEngine, ScenarioValidationError


def _engine_with_dispatch(status: int = 200, body: str = "ok") -> ScenarioEngine:
    def _dispatch(_request):
        return HTTPResponse(status_code=status, headers={"X-Request-ID": "trace123"}, body=body)

    return ScenarioEngine(
        dispatch_request=_dispatch,
        max_steps_per_scenario=10,
        max_assertions_per_step=10,
        default_seed=1337,
    )


def test_scenario_validation_rejects_invalid_method() -> None:
    engine = _engine_with_dispatch()
    payload = {
        "name": "bad",
        "steps": [{"method": "PATCH", "path": "/x", "expect": {"status": 200}}],
    }

    try:
        engine.normalize_scenario(payload)
    except ScenarioValidationError as exc:
        assert "invalid method" in str(exc)
    else:
        raise AssertionError("expected validation error")


def test_run_scenario_passes_when_assertions_match() -> None:
    engine = _engine_with_dispatch(status=201, body='{"ok":true}')
    scenario = engine.normalize_scenario(
        {
            "name": "pass",
            "steps": [
                {
                    "method": "POST",
                    "path": "/submit",
                    "expect": {"status": 201, "body_contains": ["ok"]},
                }
            ],
        }
    )

    run = engine.run_scenario(scenario, seed=123)
    assert run["status"] == "pass"
    assert run["summary"]["failed"] == 0
    assert "analysis" in run
    assert isinstance(run["analysis"].get("reliability_score"), int)


def test_run_scenario_is_deterministic_with_seed_and_chaos() -> None:
    engine = _engine_with_dispatch(status=201, body='{"ok":true}')
    scenario = engine.normalize_scenario(
        {
            "name": "chaos",
            "steps": [
                {
                    "method": "POST",
                    "path": "/submit",
                    "chaos": {
                        "enabled": True,
                        "mode": "status_flip",
                        "probability": 0.5,
                        "seed_offset": 9,
                    },
                    "expect": {"status": 201},
                }
            ],
        }
    )

    run_a = engine.run_scenario(scenario, seed=42)
    run_b = engine.run_scenario(scenario, seed=42)
    assert run_a["step_results"][0]["status"] == run_b["step_results"][0]["status"]
