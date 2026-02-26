"""Persistent store for scenarios and scenario run history."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


class ScenarioStore:
    def __init__(
        self,
        *,
        state_file: str,
        max_scenarios: int,
    ) -> None:
        self._state_file = Path(state_file)
        self._max_scenarios = max(1, max_scenarios)
        self._lock = threading.Lock()
        self._version = 1
        self._scenarios: dict[str, dict[str, Any]] = {}
        self._runs: dict[str, dict[str, Any]] = {}
        self._runs_by_scenario: dict[str, list[str]] = {}
        self._load_state()

    def list_scenarios(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._scenarios.values()]

    def get_scenario(self, scenario_id: str) -> dict[str, Any] | None:
        with self._lock:
            scenario = self._scenarios.get(scenario_id)
            if scenario is None:
                return None
            return dict(scenario)

    def create_scenario(self, scenario: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if len(self._scenarios) >= self._max_scenarios:
                raise ValueError("max_scenarios_exceeded")

            scenario_id = str(scenario["id"])
            if scenario_id in self._scenarios:
                raise ValueError("scenario_id_exists")

            if self._has_duplicate_name_locked(str(scenario["name"])):
                raise ValueError("scenario_name_exists")

            self._scenarios[scenario_id] = dict(scenario)
            self._save_locked()
            return dict(self._scenarios[scenario_id])

    def update_scenario(self, scenario_id: str, scenario: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            if scenario_id not in self._scenarios:
                return None

            if self._has_duplicate_name_locked(str(scenario["name"]), ignore_id=scenario_id):
                raise ValueError("scenario_name_exists")

            self._scenarios[scenario_id] = dict(scenario)
            self._save_locked()
            return dict(self._scenarios[scenario_id])

    def delete_scenario(self, scenario_id: str) -> bool:
        with self._lock:
            deleted = self._scenarios.pop(scenario_id, None)
            if deleted is None:
                return False

            run_ids = self._runs_by_scenario.pop(scenario_id, [])
            for run_id in run_ids:
                self._runs.pop(run_id, None)
            self._save_locked()
            return True

    def add_run(self, run: dict[str, Any]) -> None:
        with self._lock:
            run_id = str(run["run_id"])
            scenario_id = str(run["scenario_id"])
            self._runs[run_id] = dict(run)
            self._runs_by_scenario.setdefault(scenario_id, []).append(run_id)
            self._save_locked()

    def list_runs_for_scenario(self, scenario_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            ids = self._runs_by_scenario.get(scenario_id, [])
            selected = ids[-max(1, limit) :]
            return [dict(self._runs[item]) for item in reversed(selected) if item in self._runs]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return None
            return dict(run)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "version": self._version,
                "scenarios": [dict(item) for item in self._scenarios.values()],
                "runs": [dict(item) for item in self._runs.values()],
            }

    def _has_duplicate_name_locked(self, name: str, *, ignore_id: str | None = None) -> bool:
        candidate = name.strip().lower()
        for scenario in self._scenarios.values():
            if ignore_id is not None and scenario.get("id") == ignore_id:
                continue
            if str(scenario.get("name", "")).strip().lower() == candidate:
                return True
        return False

    def _load_state(self) -> None:
        if not self._state_file.exists():
            return

        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
        except Exception:
            self._backup_corrupt_file()
            return

        scenarios = data.get("scenarios", [])
        runs = data.get("runs", [])
        if not isinstance(scenarios, list) or not isinstance(runs, list):
            self._backup_corrupt_file()
            return

        for scenario in scenarios:
            if not isinstance(scenario, dict):
                continue
            scenario_id = str(scenario.get("id", "")).strip()
            if not scenario_id:
                continue
            self._scenarios[scenario_id] = scenario

        for run in runs:
            if not isinstance(run, dict):
                continue
            run_id = str(run.get("run_id", "")).strip()
            scenario_id = str(run.get("scenario_id", "")).strip()
            if not run_id or not scenario_id:
                continue
            self._runs[run_id] = run
            self._runs_by_scenario.setdefault(scenario_id, []).append(run_id)

    def _save_locked(self) -> None:
        payload = {
            "version": self._version,
            "scenarios": list(self._scenarios.values()),
            "runs": list(self._runs.values()),
        }
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._state_file.with_suffix(self._state_file.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(self._state_file)

    def _backup_corrupt_file(self) -> None:
        timestamp = int(time.time())
        backup = self._state_file.with_suffix(self._state_file.suffix + f".corrupt.{timestamp}")
        try:
            self._state_file.replace(backup)
        except OSError:
            pass


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
