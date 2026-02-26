"""HTTP handlers for scenario CRUD and run APIs."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

from request import HTTPRequest
from response import HTTPResponse
from scenario_engine import ScenarioEngine, ScenarioValidationError
from scenario_store import ScenarioStore


class ScenarioAPI:
    def __init__(
        self,
        *,
        store: ScenarioStore,
        engine: ScenarioEngine,
        emit_event: Callable[[str, str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._store = store
        self._engine = engine
        self._emit_event = emit_event

    def handle(self, request: HTTPRequest) -> tuple[HTTPResponse, dict[str, Any] | None]:
        if request.path == "/api/scenarios":
            if request.method == "GET":
                return self._ok({"scenarios": self._store.list_scenarios()}), None
            if request.method == "POST":
                return self._create_scenario(request)
            return self._method_not_allowed("GET, POST"), None

        if request.path.startswith("/api/scenarios/runs/"):
            if request.method != "GET":
                return self._method_not_allowed("GET"), None
            run_id = request.path.removeprefix("/api/scenarios/runs/")
            run = self._store.get_run(run_id)
            if run is None:
                return self._error(404, "not_found", "run not found"), None
            return self._ok({"run": run}), None

        if request.path.startswith("/api/scenarios/"):
            suffix = request.path.removeprefix("/api/scenarios/")
            if suffix.endswith("/run"):
                scenario_id = suffix.removesuffix("/run")
                if request.method != "POST":
                    return self._method_not_allowed("POST"), None
                return self._run_scenario(scenario_id, request)

            if suffix.endswith("/runs"):
                scenario_id = suffix.removesuffix("/runs")
                if request.method != "GET":
                    return self._method_not_allowed("GET"), None
                limit = 20
                limit_raw = _first_query_value(request, "limit")
                if limit_raw:
                    try:
                        limit = int(limit_raw)
                    except ValueError:
                        return self._error(400, "invalid_limit", "limit must be integer"), None
                runs = self._store.list_runs_for_scenario(scenario_id, limit=limit)
                return self._ok({"runs": runs}), None

            scenario_id = suffix
            if not scenario_id:
                return self._error(404, "not_found", "scenario not found"), None

            if request.method == "GET":
                scenario = self._store.get_scenario(scenario_id)
                if scenario is None:
                    return self._error(404, "not_found", "scenario not found"), None
                return self._ok({"scenario": scenario}), None

            if request.method == "PUT":
                return self._update_scenario(scenario_id, request)

            if request.method == "DELETE":
                deleted = self._store.delete_scenario(scenario_id)
                if not deleted:
                    return self._error(404, "not_found", "scenario not found"), None
                return self._ok({"deleted": True, "id": scenario_id}), None

            return self._method_not_allowed("GET, PUT, DELETE"), None

        return self._error(404, "not_found", "endpoint not found"), None

    def _create_scenario(self, request: HTTPRequest) -> tuple[HTTPResponse, dict[str, Any] | None]:
        payload, error = self._read_json_body(request)
        if error is not None:
            return error, None

        scenario_id = str(payload.get("id") or uuid.uuid4().hex[:12])
        try:
            normalized = self._engine.normalize_scenario(payload, scenario_id=scenario_id)
            created = self._store.create_scenario(normalized)
        except ScenarioValidationError as exc:
            return self._error(exc.status_code, exc.code, str(exc)), None
        except ValueError as exc:
            reason = str(exc)
            if reason == "max_scenarios_exceeded":
                return self._error(413, "scenario_too_large", "max scenarios reached"), None
            if reason in {"scenario_id_exists", "scenario_name_exists"}:
                return self._error(409, "duplicate_scenario", "scenario already exists"), None
            return self._error(400, "invalid_scenario", "unable to create scenario"), None

        return self._json(201, {"scenario": created}), None

    def _update_scenario(
        self,
        scenario_id: str,
        request: HTTPRequest,
    ) -> tuple[HTTPResponse, dict[str, Any] | None]:
        payload, error = self._read_json_body(request)
        if error is not None:
            return error, None

        existing = self._store.get_scenario(scenario_id)
        if existing is None:
            return self._error(404, "not_found", "scenario not found"), None

        try:
            normalized = self._engine.normalize_scenario(
                payload,
                scenario_id=scenario_id,
                created_at=str(existing.get("created_at", "")) or None,
            )
            updated = self._store.update_scenario(scenario_id, normalized)
        except ScenarioValidationError as exc:
            return self._error(exc.status_code, exc.code, str(exc)), None
        except ValueError as exc:
            if str(exc) == "scenario_name_exists":
                return self._error(409, "duplicate_scenario", "scenario already exists"), None
            return self._error(400, "invalid_scenario", "unable to update scenario"), None

        if updated is None:
            return self._error(404, "not_found", "scenario not found"), None
        return self._ok({"scenario": updated}), None

    def _run_scenario(
        self,
        scenario_id: str,
        request: HTTPRequest,
    ) -> tuple[HTTPResponse, dict[str, Any] | None]:
        scenario = self._store.get_scenario(scenario_id)
        if scenario is None:
            return self._error(404, "not_found", "scenario not found"), None

        payload, error = self._read_json_body(request, allow_empty=True)
        if error is not None:
            return error, None

        seed_raw = payload.get("seed") if payload is not None else None
        seed = None
        if seed_raw is not None:
            try:
                seed = int(seed_raw)
            except ValueError:
                return self._error(400, "invalid_seed", "seed must be integer"), None

        run = self._engine.run_scenario(
            scenario,
            seed=seed,
            on_event=self._emit_event,
        )
        self._store.add_run(run)
        response = self._ok({"run": run})
        response.headers["X-Scenario-Run-ID"] = str(run["run_id"])
        response.headers["X-Scenario-Seed"] = str(run["seed"])
        response.headers["X-Scenario-ID"] = str(scenario_id)
        response.headers["X-Scenario-Status"] = str(run["status"])
        return response, run

    def _read_json_body(
        self,
        request: HTTPRequest,
        *,
        allow_empty: bool = False,
    ) -> tuple[dict[str, Any], HTTPResponse | None]:
        if not request.body and allow_empty:
            return {}, None

        try:
            decoded = request.body.decode("utf-8") if request.body else "{}"
            payload = json.loads(decoded)
        except UnicodeDecodeError:
            return {}, self._error(400, "invalid_json", "body must be utf-8")
        except json.JSONDecodeError:
            return {}, self._error(400, "invalid_json", "body must be valid json")

        if not isinstance(payload, dict):
            return {}, self._error(400, "invalid_json", "top-level json must be an object")
        return payload, None

    def _ok(self, payload: dict[str, Any]) -> HTTPResponse:
        return self._json(200, payload)

    def _json(self, status: int, payload: dict[str, Any]) -> HTTPResponse:
        return HTTPResponse(
            status_code=status,
            headers={"Content-Type": "application/json"},
            body=json.dumps(payload, sort_keys=True),
        )

    def _error(self, status: int, code: str, message: str) -> HTTPResponse:
        return self._json(status, {"error": {"code": code, "message": message}})

    def _method_not_allowed(self, allowed: str) -> HTTPResponse:
        return HTTPResponse(
            status_code=405,
            headers={"Allow": allowed, "Content-Type": "application/json"},
            body=json.dumps(
                {
                    "error": {
                        "code": "method_not_allowed",
                        "message": "method not allowed",
                    }
                }
            ),
        )


def _first_query_value(request: HTTPRequest, key: str) -> str:
    values = request.query_params.get(key, [])
    if not values:
        return ""
    return values[0]
