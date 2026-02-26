"""CLI tests for scenario runner tool."""

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from server import HTTPServer


def _start_server(tmp_path: Path) -> tuple[HTTPServer, threading.Thread]:
    server = HTTPServer(
        port=0,
        engine="selectors",
        enable_tls=False,
        enable_playground=True,
        enable_scenarios=True,
        state_file=str(tmp_path / "playground.json"),
        scenario_state_file=str(tmp_path / "scenarios.json"),
        target_state_file=str(tmp_path / "targets.json"),
    )
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()

    deadline = time.time() + 3
    while server.port == 0 and time.time() < deadline:
        time.sleep(0.01)
    if server.port == 0:
        raise RuntimeError("server did not start")
    return server, thread


def _stop_server(server: HTTPServer, thread: threading.Thread) -> None:
    server.stop()
    thread.join(timeout=3)


class _FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = b'{"ok":true,"fixture":"yes"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        _ = (format, args)


def _start_fixture_server() -> tuple[ThreadingHTTPServer, threading.Thread]:
    fixture = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    thread = threading.Thread(target=fixture.serve_forever, daemon=True)
    thread.start()
    return fixture, thread


def _stop_fixture_server(server: ThreadingHTTPServer, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _create_target(server_port: int, *, base_url: str) -> str:
    payload = {
        "name": "cli-fixture",
        "base_url": base_url,
        "enabled": True,
        "timeout_ms": 3000,
    }
    request = urllib.request.Request(
        url=f"http://127.0.0.1:{server_port}/api/targets",
        method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload).encode("utf-8"),
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        body = json.loads(response.read().decode("utf-8"))
        return str(body["target"]["id"])


def test_cli_run_local_scenario_file(tmp_path: Path) -> None:
    server, thread = _start_server(tmp_path)
    scenario_file = tmp_path / "scenario.json"
    scenario_file.write_text(
        json.dumps(
            {
                "name": "cli-flow",
                "steps": [
                    {
                        "method": "GET",
                        "path": "/",
                        "expect": {"status": 200, "body_contains": ["Hello World"]},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    try:
        result = subprocess.run(
            [
                "python3",
                "tools/scenario_runner.py",
                "run",
                str(scenario_file),
                "--base-url",
                f"http://127.0.0.1:{server.port}",
                "--json",
            ],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        _stop_server(server, thread)

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "pass"


def test_cli_run_live_emits_step_progress(tmp_path: Path) -> None:
    server, thread = _start_server(tmp_path)
    scenario_file = tmp_path / "scenario-live.json"
    scenario_file.write_text(
        json.dumps(
            {
                "name": "cli-live-flow",
                "steps": [
                    {
                        "id": "step-home",
                        "method": "GET",
                        "path": "/",
                        "expect": {"status": 200, "body_contains": ["Hello World"]},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    try:
        result = subprocess.run(
            [
                "python3",
                "tools/scenario_runner.py",
                "run",
                str(scenario_file),
                "--base-url",
                f"http://127.0.0.1:{server.port}",
                "--live",
            ],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        _stop_server(server, thread)

    assert result.returncode == 0
    assert "STEP START" in result.stdout
    assert "STEP END" in result.stdout


def test_cli_proxy_command(tmp_path: Path) -> None:
    fixture, fixture_thread = _start_fixture_server()
    server, thread = _start_server(tmp_path)
    try:
        target_id = _create_target(
            server.port,
            base_url=f"http://127.0.0.1:{fixture.server_address[1]}",
        )
        result = subprocess.run(
            [
                "python3",
                "tools/scenario_runner.py",
                "proxy",
                "--server",
                f"http://127.0.0.1:{server.port}",
                "--target-id",
                target_id,
                "--method",
                "GET",
                "--path",
                "/",
            ],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        _stop_server(server, thread)
        _stop_fixture_server(fixture, fixture_thread)

    assert result.returncode == 0
    assert "Proxy response |" in result.stdout
