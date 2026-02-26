"""Scenario validation and execution engine."""

from __future__ import annotations

import random
import time
import uuid
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from request import HTTPRequest
from response import HTTPResponse
from scenario_store import utc_now

DispatchFn = Callable[[HTTPRequest], HTTPResponse]
EventCallback = Callable[[str, str, dict[str, Any]], None]

ALLOWED_METHODS = {"GET", "POST", "PUT", "DELETE"}
ALLOWED_CHAOS_MODES = {"none", "status_flip", "drop_body", "fixed_error"}


class ScenarioValidationError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 400,
        code: str = "invalid_scenario",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class ScenarioEngine:
    def __init__(
        self,
        *,
        dispatch_request: DispatchFn,
        max_steps_per_scenario: int,
        max_assertions_per_step: int,
        default_seed: int,
    ) -> None:
        self._dispatch_request = dispatch_request
        self._max_steps_per_scenario = max_steps_per_scenario
        self._max_assertions_per_step = max_assertions_per_step
        self._default_seed = default_seed

    def normalize_scenario(
        self,
        payload: dict[str, Any],
        *,
        scenario_id: str | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        name = str(payload.get("name", "")).strip()
        if not name:
            raise ScenarioValidationError("scenario name is required")

        description = str(payload.get("description", ""))
        steps_raw = payload.get("steps", [])
        if not isinstance(steps_raw, list):
            raise ScenarioValidationError("steps must be a list")
        if not steps_raw:
            raise ScenarioValidationError("scenario must contain at least one step")
        if len(steps_raw) > self._max_steps_per_scenario:
            raise ScenarioValidationError(
                "scenario has too many steps",
                status_code=413,
                code="scenario_too_large",
            )

        defaults_raw = payload.get("defaults", {})
        if defaults_raw is None:
            defaults_raw = {}
        if not isinstance(defaults_raw, dict):
            raise ScenarioValidationError("defaults must be an object")

        defaults = {
            "stop_on_fail": bool(defaults_raw.get("stop_on_fail", True)),
        }

        normalized_steps: list[dict[str, Any]] = []
        for index, step_raw in enumerate(steps_raw):
            normalized_steps.append(self._normalize_step(step_raw, index=index))

        now = utc_now()
        return {
            "id": scenario_id or str(payload.get("id") or uuid.uuid4().hex[:12]),
            "name": name,
            "description": description,
            "created_at": created_at or now,
            "updated_at": now,
            "steps": normalized_steps,
            "defaults": defaults,
        }

    def run_scenario(
        self,
        scenario: dict[str, Any],
        *,
        seed: int | None = None,
        on_event: EventCallback | None = None,
    ) -> dict[str, Any]:
        started_perf = time.perf_counter()
        started_at = utc_now()
        run_seed = self._default_seed if seed is None else int(seed)

        step_results: list[dict[str, Any]] = []
        failed_assertions = 0
        passed_steps = 0
        failed_steps = 0

        stop_on_fail = bool(scenario.get("defaults", {}).get("stop_on_fail", True))
        if on_event is not None:
            on_event(
                "scenario.run.started",
                "",
                {
                    "scenario_id": str(scenario.get("id", "")),
                    "seed": run_seed,
                },
            )

        for idx, step in enumerate(scenario.get("steps", [])):
            target_id = str(step.get("target_id", "local"))
            if on_event is not None:
                on_event(
                    "scenario.step.started",
                    "",
                    {
                        "scenario_id": str(scenario.get("id", "")),
                        "step_id": str(step.get("id", "")),
                        "target_id": target_id,
                    },
                )
            if int(step.get("delay_ms", 0)) > 0:
                time.sleep(int(step["delay_ms"]) / 1000)

            request = self._request_from_step(step)
            step_started = time.perf_counter()
            try:
                response = self._dispatch_with_optional_target(request, target_id=target_id)
            except Exception as exc:  # pragma: no cover - defensive path
                response = HTTPResponse(
                    status_code=502,
                    headers={"Content-Type": "text/plain; charset=utf-8"},
                    body=f"target dispatch error: {exc}",
                )
            latency_ms = (time.perf_counter() - step_started) * 1000

            chaos_applied, response_view = self._apply_chaos(
                response,
                chaos=step.get("chaos"),
                seed=run_seed,
                step_index=idx,
            )
            assertion_results, failure_reason = self._evaluate_expectations(
                step=step,
                response_view=response_view,
                latency_ms=latency_ms,
            )

            if failure_reason is None:
                status = "pass"
                passed_steps += 1
            else:
                status = "fail"
                failed_steps += 1
                failed_assertions += sum(1 for item in assertion_results if not item["passed"])

            trace_id = str(response.headers.get("X-Request-ID", ""))
            step_results.append(
                {
                    "step_id": step["id"],
                    "status": status,
                    "request": {
                        "method": request.method,
                        "path": request.path,
                        "headers": dict(request.headers),
                        "body": request.body.decode("utf-8", errors="replace"),
                        "target_id": target_id,
                    },
                    "response": response_view,
                    "assertions": assertion_results,
                    "latency_ms": round(latency_ms, 3),
                    "trace_id": trace_id,
                    "failure_reason": failure_reason,
                    "chaos_applied": chaos_applied,
                }
            )

            if failure_reason is not None and stop_on_fail:
                if on_event is not None:
                    on_event(
                        "scenario.step.completed",
                        trace_id,
                        {
                            "scenario_id": str(scenario.get("id", "")),
                            "step_id": str(step.get("id", "")),
                            "status": status,
                            "failure_reason": failure_reason,
                            "latency_ms": round(latency_ms, 3),
                            "target_id": target_id,
                        },
                    )
                break
            if on_event is not None:
                on_event(
                    "scenario.step.completed",
                    trace_id,
                    {
                        "scenario_id": str(scenario.get("id", "")),
                        "step_id": str(step.get("id", "")),
                        "status": status,
                        "failure_reason": failure_reason,
                        "latency_ms": round(latency_ms, 3),
                        "target_id": target_id,
                    },
                )

        duration_ms = (time.perf_counter() - started_perf) * 1000
        status = "pass" if failed_steps == 0 else "fail"
        run_id = uuid.uuid4().hex[:14]

        run_report = {
            "run_id": run_id,
            "scenario_id": str(scenario["id"]),
            "started_at": started_at,
            "finished_at": utc_now(),
            "seed": run_seed,
            "status": status,
            "step_results": step_results,
            "summary": {
                "passed": passed_steps,
                "failed": failed_steps,
                "duration_ms": round(duration_ms, 3),
            },
            "failed_assertions": failed_assertions,
        }
        run_report["analysis"] = self._build_analysis(run_report)
        if on_event is not None:
            on_event(
                "scenario.run.completed",
                "",
                {
                    "scenario_id": str(scenario.get("id", "")),
                    "run_id": run_id,
                    "status": status,
                    "summary": dict(run_report["summary"]),
                    "seed": run_seed,
                },
            )
        return run_report

    def _build_analysis(self, run: dict[str, Any]) -> dict[str, Any]:
        step_results = list(run.get("step_results", []))
        failed_step = next((step for step in step_results if step.get("status") == "fail"), None)
        bottleneck = max(
            step_results,
            key=lambda step: float(step.get("latency_ms", 0.0)),
            default=None,
        )

        failed_assertions = int(run.get("failed_assertions", 0))
        summary = run.get("summary", {})
        failed_steps = int(summary.get("failed", 0))
        max_latency = float(bottleneck.get("latency_ms", 0.0) if bottleneck else 0.0)

        score = 100
        score -= failed_steps * 25
        score -= failed_assertions * 5
        score -= min(20, int(max_latency // 100))
        score = max(0, min(100, score))

        if failed_step is None:
            root_cause = "No assertion failures detected."
            suggested = (
                "Increase chaos severity or tighten assertions to stress "
                "the system further."
            )
        else:
            reason = str(failed_step.get("failure_reason", "unknown failure"))
            root_cause = f"Step {failed_step.get('step_id')} failed: {reason}"
            if "status expected" in reason:
                suggested = (
                    "Investigate routing, target health, or incident profile "
                    "causing status drift."
                )
            elif "latency" in reason:
                suggested = (
                    "Reduce latency by isolating slow dependency and applying "
                    "fallback profile."
                )
            elif "header" in reason:
                suggested = (
                    "Validate middleware/header normalization across target "
                    "and local handlers."
                )
            else:
                suggested = (
                    "Inspect trace and response payload for deterministic "
                    "failure source."
                )

        bottleneck_route = ""
        if bottleneck is not None:
            req = bottleneck.get("request", {})
            bottleneck_route = f"{req.get('method', '-')} {req.get('path', '-')}"

        return {
            "root_cause_summary": root_cause,
            "failing_step_id": "" if failed_step is None else str(failed_step.get("step_id", "")),
            "failing_step_reason": (
                ""
                if failed_step is None
                else str(failed_step.get("failure_reason", ""))
            ),
            "latency_bottleneck_route": bottleneck_route,
            "latency_bottleneck_ms": round(max_latency, 3),
            "reliability_score": score,
            "suggested_next_action": suggested,
            "status": str(run.get("status", "fail")),
        }

    def _normalize_step(self, step_raw: Any, *, index: int) -> dict[str, Any]:
        if not isinstance(step_raw, dict):
            raise ScenarioValidationError(f"step {index + 1} must be an object")

        method = str(step_raw.get("method", "")).upper()
        if method not in ALLOWED_METHODS:
            raise ScenarioValidationError(f"step {index + 1}: invalid method")

        path = str(step_raw.get("path", "")).strip()
        if not path.startswith("/"):
            raise ScenarioValidationError(f"step {index + 1}: path must start with /")

        headers = step_raw.get("headers", {})
        if headers is None:
            headers = {}
        if not isinstance(headers, dict):
            raise ScenarioValidationError(f"step {index + 1}: headers must be an object")

        body = str(step_raw.get("body", ""))

        delay_ms = int(step_raw.get("delay_ms", 0))
        if delay_ms < 0:
            raise ScenarioValidationError(f"step {index + 1}: delay_ms must be >= 0")

        chaos = self._normalize_chaos(step_raw.get("chaos"), index=index)

        expect = step_raw.get("expect", {})
        if expect is None:
            expect = {}
        if not isinstance(expect, dict):
            raise ScenarioValidationError(f"step {index + 1}: expect must be an object")

        expectation = self._normalize_expect(expect, index=index)
        total_assertions = (
            1
            + len(expectation["body_contains"])
            + len(expectation["header_equals"])
            + (1 if expectation["max_latency_ms"] is not None else 0)
        )
        if total_assertions > self._max_assertions_per_step:
            raise ScenarioValidationError(
                f"step {index + 1}: too many assertions",
                status_code=413,
                code="scenario_too_large",
            )

        return {
            "id": str(step_raw.get("id") or f"step-{index + 1}"),
            "name": str(step_raw.get("name") or f"Step {index + 1}"),
            "method": method,
            "path": path,
            "target_id": str(step_raw.get("target_id", "local")),
            "headers": {str(k): str(v) for k, v in headers.items()},
            "body": body,
            "delay_ms": delay_ms,
            "chaos": chaos,
            "expect": expectation,
        }

    def _dispatch_with_optional_target(
        self,
        request: HTTPRequest,
        *,
        target_id: str,
    ) -> HTTPResponse:
        try:
            return self._dispatch_request(request, target_id=target_id)
        except TypeError:
            return self._dispatch_request(request)

    def _normalize_expect(self, expect_raw: dict[str, Any], *, index: int) -> dict[str, Any]:
        status_raw = expect_raw.get("status", 200)
        if isinstance(status_raw, list):
            if not status_raw:
                raise ScenarioValidationError(f"step {index + 1}: status list cannot be empty")
            allowed_status = [int(item) for item in status_raw]
        else:
            allowed_status = [int(status_raw)]

        body_contains_raw = expect_raw.get("body_contains", [])
        if body_contains_raw is None:
            body_contains_raw = []
        if not isinstance(body_contains_raw, list):
            raise ScenarioValidationError(f"step {index + 1}: body_contains must be a list")

        header_equals_raw = expect_raw.get("header_equals", {})
        if header_equals_raw is None:
            header_equals_raw = {}
        if not isinstance(header_equals_raw, dict):
            raise ScenarioValidationError(f"step {index + 1}: header_equals must be an object")

        max_latency_ms_raw = expect_raw.get("max_latency_ms")
        max_latency_ms = None if max_latency_ms_raw is None else int(max_latency_ms_raw)

        return {
            "status": allowed_status,
            "body_contains": [str(item) for item in body_contains_raw],
            "header_equals": {
                str(k).lower(): str(v)
                for k, v in header_equals_raw.items()
            },
            "max_latency_ms": max_latency_ms,
        }

    def _normalize_chaos(self, chaos_raw: Any, *, index: int) -> dict[str, Any] | None:
        if chaos_raw is None:
            return None
        if not isinstance(chaos_raw, dict):
            raise ScenarioValidationError(f"step {index + 1}: chaos must be an object")

        enabled = bool(chaos_raw.get("enabled", False))
        mode = str(chaos_raw.get("mode", "none"))
        if mode not in ALLOWED_CHAOS_MODES:
            raise ScenarioValidationError(f"step {index + 1}: invalid chaos mode")

        probability = float(chaos_raw.get("probability", 0.0))
        if probability < 0.0 or probability > 1.0:
            raise ScenarioValidationError(f"step {index + 1}: probability must be between 0 and 1")

        seed_offset = int(chaos_raw.get("seed_offset", 0))

        return {
            "enabled": enabled,
            "mode": mode,
            "probability": probability,
            "seed_offset": seed_offset,
        }

    def _request_from_step(self, step: dict[str, Any]) -> HTTPRequest:
        parsed = urlsplit(step["path"])
        path = parsed.path or "/"
        query = parse_qs(parsed.query, keep_blank_values=True)
        headers = {str(k).lower(): str(v) for k, v in step.get("headers", {}).items()}
        headers.setdefault("host", "localhost")
        body_bytes = str(step.get("body", "")).encode("utf-8")
        return HTTPRequest(
            method=step["method"],
            path=path,
            raw_target=step["path"],
            http_version="HTTP/1.1",
            headers=headers,
            body=body_bytes,
            query_params=query,
            keep_alive=False,
            transfer_encoding=None,
        )

    def _apply_chaos(
        self,
        response: HTTPResponse,
        *,
        chaos: dict[str, Any] | None,
        seed: int,
        step_index: int,
    ) -> tuple[bool, dict[str, Any]]:
        view = {
            "status": response.status_code,
            "headers": {str(k).lower(): str(v) for k, v in response.headers.items()},
            "body": self._response_body_text(response),
        }

        if chaos is None or not chaos.get("enabled", False):
            return False, view

        rng = random.Random(seed + int(chaos.get("seed_offset", 0)) + step_index)
        if rng.random() > float(chaos.get("probability", 0.0)):
            return False, view

        mode = str(chaos.get("mode", "none"))
        if mode == "status_flip":
            view["status"] = 503 if int(view["status"]) < 500 else 200
        elif mode == "drop_body":
            view["body"] = ""
        elif mode == "fixed_error":
            view["status"] = 500
            view["body"] = "chaos injected error"
            view["headers"]["content-type"] = "text/plain; charset=utf-8"

        return True, view

    def _evaluate_expectations(
        self,
        *,
        step: dict[str, Any],
        response_view: dict[str, Any],
        latency_ms: float,
    ) -> tuple[list[dict[str, Any]], str | None]:
        expect = step["expect"]
        assertions: list[dict[str, Any]] = []
        failures: list[str] = []

        allowed_status = [int(item) for item in expect["status"]]
        actual_status = int(response_view["status"])
        status_pass = actual_status in allowed_status
        assertions.append(
            {
                "type": "status",
                "passed": status_pass,
                "expected": allowed_status,
                "actual": actual_status,
            }
        )
        if not status_pass:
            failures.append(f"status expected {allowed_status} got {actual_status}")

        body_text = str(response_view.get("body", ""))
        for token in expect["body_contains"]:
            passed = token in body_text
            assertions.append(
                {
                    "type": "body_contains",
                    "passed": passed,
                    "expected": token,
                    "actual": body_text,
                }
            )
            if not passed:
                failures.append(f"body missing token: {token}")

        headers = response_view.get("headers", {})
        for key, value in expect["header_equals"].items():
            actual = str(headers.get(key, ""))
            passed = actual == value
            assertions.append(
                {
                    "type": "header_equals",
                    "passed": passed,
                    "expected": {key: value},
                    "actual": {key: actual},
                }
            )
            if not passed:
                failures.append(f"header {key} expected {value} got {actual}")

        max_latency_ms = expect.get("max_latency_ms")
        if max_latency_ms is not None:
            passed = latency_ms <= float(max_latency_ms)
            assertions.append(
                {
                    "type": "max_latency_ms",
                    "passed": passed,
                    "expected": float(max_latency_ms),
                    "actual": round(latency_ms, 3),
                }
            )
            if not passed:
                failures.append(
                    f"latency {round(latency_ms, 3)}ms exceeded {float(max_latency_ms)}ms"
                )

        failure_reason = None if not failures else failures[0]
        return assertions, failure_reason

    def _response_body_text(self, response: HTTPResponse) -> str:
        if isinstance(response.body, bytes):
            return response.body.decode("utf-8", errors="replace")
        if isinstance(response.body, str):
            return response.body
        return ""
