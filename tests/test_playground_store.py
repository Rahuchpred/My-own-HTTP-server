"""Unit tests for playground persistent store."""

from __future__ import annotations

import json
from pathlib import Path

from playground_store import PlaygroundStore


def test_store_persists_mocks_and_trims_history(tmp_path: Path) -> None:
    state_file = tmp_path / "server_state.json"
    store = PlaygroundStore(state_file=str(state_file), history_limit=2, max_mock_routes=10)

    created = store.add_mock(
        {
            "id": "m1",
            "method": "POST",
            "path_pattern": "/api/users",
            "status": 201,
            "headers": {"X-Mock": "yes"},
            "body": '{"ok":true}',
            "content_type": "application/json",
        }
    )
    assert created["id"] == "m1"

    store.add_history({"request_id": "a", "path": "/a"})
    store.add_history({"request_id": "b", "path": "/b"})
    store.add_history({"request_id": "c", "path": "/c"})

    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert len(data["mocks"]) == 1
    assert [item["request_id"] for item in data["history"]] == ["b", "c"]

    reloaded = PlaygroundStore(state_file=str(state_file), history_limit=2, max_mock_routes=10)
    assert len(reloaded.list_mocks()) == 1
    assert [item["request_id"] for item in reloaded.list_history()] == ["b", "c"]


def test_store_backs_up_corrupt_state_file(tmp_path: Path) -> None:
    state_file = tmp_path / "broken_state.json"
    state_file.write_text("{not-json", encoding="utf-8")

    store = PlaygroundStore(state_file=str(state_file), history_limit=10, max_mock_routes=10)

    assert store.list_mocks() == []
    backups = list(tmp_path.glob("broken_state.json.corrupt.*"))
    assert backups
    assert not state_file.exists()
