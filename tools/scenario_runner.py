#!/usr/bin/env python3
"""CLI scenario runner for local files and remote stored scenarios."""
# ruff: noqa: E402

from __future__ import annotations

import argparse
import curses
import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import CLI_REFRESH_MS_DEFAULT
from response import HTTPResponse
from scenario_engine import ScenarioEngine, ScenarioValidationError

EVENT_TOPICS = [
    "request.completed",
    "scenario.run.started",
    "scenario.step.started",
    "scenario.step.completed",
    "scenario.run.completed",
    "proxy.request.completed",
    "system.health",
]


def _create_unverified_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _with_token_headers(headers: dict[str, str], token: str | None) -> dict[str, str]:
    merged = dict(headers)
    if token:
        merged["Authorization"] = f"Bearer {token}"
    return merged


def _request_json(
    *,
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    token: str | None = None,
    timeout_secs: int = 10,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        url=url,
        method=method,
        headers=_with_token_headers(headers, token),
        data=data,
    )
    try:
        with urllib.request.urlopen(
            req,
            context=_create_unverified_context(),
            timeout=timeout_secs,
        ) as resp:
            body = resp.read().decode("utf-8")
            parsed = json.loads(body) if body else {}
            return int(resp.status), dict(resp.headers.items()), parsed
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError:
            parsed = {"error": {"code": "http_error", "message": body}}
        return int(exc.code), dict(exc.headers.items()), parsed


def _request_raw_http(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout_secs: int,
) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url=url, method=method, headers=headers, data=body)
    try:
        with urllib.request.urlopen(
            req,
            context=_create_unverified_context(),
            timeout=timeout_secs,
        ) as resp:
            return int(resp.status), dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), dict(exc.headers.items()), exc.read()


def _dispatch_via_http(base_url: str, token: str | None = None):
    def _dispatch(request, *, target_id: str = "local") -> HTTPResponse:
        if target_id != "local":
            status, _, payload = _request_json(
                method="POST",
                url=f"{base_url.rstrip('/')}/api/proxy/request",
                payload={
                    "target_id": target_id,
                    "method": request.method,
                    "path": request.raw_target,
                    "headers": dict(request.headers),
                    "body": request.body.decode("utf-8", errors="replace"),
                },
                token=token,
            )
            response_payload = payload.get("response", {})
            return HTTPResponse(
                status_code=int(response_payload.get("status", status)),
                headers={str(k): str(v) for k, v in response_payload.get("headers", {}).items()},
                body=str(response_payload.get("body", "")),
            )

        url = f"{base_url.rstrip('/')}{request.raw_target}"
        headers = _with_token_headers({str(k): str(v) for k, v in request.headers.items()}, token)
        body = request.body if request.body else None
        status, response_headers, response_body = _request_raw_http(
            method=request.method,
            url=url,
            headers=headers,
            body=body,
            timeout_secs=10,
        )
        return HTTPResponse(
            status_code=status,
            headers=response_headers,
            body=response_body,
        )

    return _dispatch


def _print_pretty(run: dict[str, Any]) -> None:
    summary = run.get("summary", {})
    print(
        f"Run: {run.get('run_id')} | "
        f"Scenario: {run.get('scenario_id')} | Seed: {run.get('seed')}"
    )
    print(
        f"Status: {run.get('status')} | Passed: {summary.get('passed')} | "
        f"Failed: {summary.get('failed')} | Duration: {summary.get('duration_ms')}ms"
    )
    print("-" * 72)
    for step in run.get("step_results", []):
        print(
            f"{step.get('status').upper():4} {step.get('step_id'):12} "
            f"latency={step.get('latency_ms')}ms trace={step.get('trace_id')}"
        )
        if step.get("failure_reason"):
            print(f"      reason: {step.get('failure_reason')}")


def _cmd_run(args: argparse.Namespace) -> int:
    scenario_path = Path(args.file)
    if not scenario_path.exists():
        print(f"Scenario file not found: {scenario_path}", file=sys.stderr)
        return 1

    payload = json.loads(scenario_path.read_text(encoding="utf-8"))
    engine = ScenarioEngine(
        dispatch_request=_dispatch_via_http(args.base_url, token=args.token),
        max_steps_per_scenario=args.max_steps,
        max_assertions_per_step=args.max_assertions,
        default_seed=args.seed if args.seed is not None else 1337,
    )

    try:
        scenario = engine.normalize_scenario(payload)
    except ScenarioValidationError as exc:
        print(f"Scenario validation error: {exc}", file=sys.stderr)
        return 2

    def _live_event(event_type: str, trace_id: str, event_payload: dict[str, Any]) -> None:
        if not args.live:
            return
        if event_type == "scenario.step.started":
            print(
                "STEP START "
                f"scenario={event_payload.get('scenario_id')} "
                f"step={event_payload.get('step_id')} "
                f"target={event_payload.get('target_id', 'local')}"
            )
        elif event_type == "scenario.step.completed":
            status = event_payload.get("status", "-")
            reason = event_payload.get("failure_reason")
            parts = [
                "STEP END",
                f"step={event_payload.get('step_id')}",
                f"status={status}",
                f"latency_ms={event_payload.get('latency_ms')}",
            ]
            if trace_id:
                parts.append(f"trace={trace_id}")
            if reason:
                parts.append(f"reason={reason}")
            print(" ".join(parts))

    run = engine.run_scenario(
        scenario,
        seed=args.seed,
        on_event=_live_event if args.live else None,
    )

    if args.json:
        print(json.dumps(run, indent=2, sort_keys=True))
    else:
        _print_pretty(run)

    return 0 if run.get("status") == "pass" else 3


def _cmd_list(args: argparse.Namespace) -> int:
    url = f"{args.server.rstrip('/')}/api/scenarios"
    status, _headers, payload = _request_json(method="GET", url=url, token=args.token)
    if status != 200:
        print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
        return 1

    scenarios = payload.get("scenarios", [])
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    for item in scenarios:
        print(f"{item.get('id')}\t{item.get('name')}\tsteps={len(item.get('steps', []))}")
    return 0


def _cmd_run_remote(args: argparse.Namespace) -> int:
    url = f"{args.server.rstrip('/')}/api/scenarios/{args.scenario_id}/run"
    body: dict[str, Any] = {}
    if args.seed is not None:
        body["seed"] = args.seed

    status, _headers, payload = _request_json(
        method="POST",
        url=url,
        payload=body,
        token=args.token,
    )
    if status != 200:
        print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
        return 1

    run = payload.get("run", {})
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if run.get("status") == "pass" else 3

    _print_pretty(run)
    return 0 if run.get("status") == "pass" else 3


def _reduce_events(
    state: dict[str, Any],
    *,
    events: list[dict[str, Any]],
    failed_only: bool,
) -> None:
    for event in events:
        state["last_event_id"] = max(int(state["last_event_id"]), int(event.get("id", 0)))
        event_type = str(event.get("type", ""))
        payload = event.get("payload", {}) if isinstance(event.get("payload", {}), dict) else {}
        trace_id = str(event.get("trace_id", ""))

        if event_type == "request.completed":
            row = {
                "status": payload.get("status"),
                "method": payload.get("method"),
                "path": payload.get("path"),
                "latency_ms": payload.get("latency_ms"),
                "trace_id": trace_id,
            }
            if (not failed_only) or int(row.get("status", 0) or 0) >= 400:
                state["requests"].insert(0, row)
                state["requests"] = state["requests"][:20]
        elif event_type.startswith("scenario."):
            row = {
                "type": event_type,
                "scenario_id": payload.get("scenario_id", ""),
                "step_id": payload.get("step_id", ""),
                "status": payload.get("status", ""),
                "reason": payload.get("failure_reason", ""),
                "trace_id": trace_id,
            }
            if (not failed_only) or row.get("status") == "fail":
                state["scenario"].insert(0, row)
                state["scenario"] = state["scenario"][:20]
        elif event_type == "proxy.request.completed":
            row = {
                "target_id": payload.get("target_id", ""),
                "status": payload.get("status", ""),
                "method": payload.get("method", ""),
                "path": payload.get("path", ""),
                "trace_id": trace_id,
            }
            if (not failed_only) or int(row.get("status", 0) or 0) >= 400:
                state["proxy"].insert(0, row)
                state["proxy"] = state["proxy"][:20]
        elif event_type == "system.health":
            state["health"] = {
                "ts": event.get("ts", ""),
                **payload,
            }


def _fetch_live_snapshot(
    *,
    server: str,
    token: str | None,
    since_id: int,
    limit: int = 300,
) -> tuple[int, dict[str, Any]]:
    params = {
        "since_id": str(since_id),
        "limit": str(limit),
        "topics": ",".join(EVENT_TOPICS),
    }
    if token:
        params["token"] = token
    query = urllib.parse.urlencode(params)
    status, _headers, payload = _request_json(
        method="GET",
        url=f"{server.rstrip('/')}/api/events/snapshot?{query}",
        token=token,
    )
    return status, payload


def _fetch_metrics(*, server: str, token: str | None) -> dict[str, Any]:
    status, _headers, payload = _request_json(
        method="GET",
        url=f"{server.rstrip('/')}/_metrics",
        token=token,
    )
    if status != 200:
        return {}
    return payload


def _render_live_text(state: dict[str, Any], metrics: dict[str, Any], failed_only: bool) -> str:
    lines = []
    lines.append(
        "Live Ops | "
        f"cursor={state['last_event_id']} "
        f"failed_only={'on' if failed_only else 'off'} "
        "(Ctrl+C to stop)"
    )
    lines.append(
        "Summary | "
        f"requests={metrics.get('total_requests', 0)} "
        f"p99={metrics.get('latency_p99_ms', 0)}ms "
        f"open={metrics.get('open_connections', 0)} "
        f"drain={metrics.get('drain_state', '-') }"
    )
    lines.append("")
    lines.append("Recent Requests")
    for row in state["requests"][:10]:
        lines.append(
            f"  {row['status']} {row['method']} {row['path']} "
            f"lat={row['latency_ms']}ms trace={row['trace_id']}"
        )
    lines.append("")
    lines.append("Scenario Timeline")
    for row in state["scenario"][:10]:
        reason = f" reason={row['reason']}" if row.get("reason") else ""
        lines.append(
            f"  {row['type']} scenario={row['scenario_id']} step={row['step_id']} "
            f"status={row['status']}{reason}"
        )
    lines.append("")
    lines.append("Proxy Activity")
    for row in state["proxy"][:10]:
        lines.append(
            f"  {row['status']} {row['method']} {row['path']} "
            f"target={row['target_id']} trace={row['trace_id']}"
        )
    lines.append("")
    lines.append("Health")
    for key in sorted(state["health"].keys()):
        lines.append(f"  {key}: {state['health'][key]}")
    return "\n".join(lines)


def _run_live_curses(stdscr: curses.window, args: argparse.Namespace) -> int:
    curses.curs_set(0)
    stdscr.nodelay(True)

    failed_only = bool(args.failed_only)
    state = {
        "last_event_id": 0,
        "requests": [],
        "scenario": [],
        "proxy": [],
        "health": {},
    }
    metrics: dict[str, Any] = {}
    refresh_secs = max(0.05, args.refresh_ms / 1000)
    metric_counter = 0

    while True:
        key = stdscr.getch()
        if key in (ord("q"), ord("Q")):
            return 0
        if key in (ord("f"), ord("F")):
            failed_only = not failed_only
        if key in (ord("c"), ord("C")):
            state["requests"].clear()
            state["scenario"].clear()
            state["proxy"].clear()
        if key in (ord("r"), ord("R")):
            state["last_event_id"] = 0

        status, payload = _fetch_live_snapshot(
            server=args.server,
            token=args.token,
            since_id=int(state["last_event_id"]),
        )
        if status == 200:
            if payload.get("overflowed"):
                state["last_event_id"] = int(payload.get("oldest_id", 0))
            _reduce_events(
                state,
                events=list(payload.get("events", [])),
                failed_only=failed_only,
            )
        else:
            state["health"] = {
                "state": "snapshot_error",
                "status": status,
                "payload": payload,
            }

        metric_counter += 1
        if metric_counter >= max(1, int(1000 / max(1, args.refresh_ms))):
            metrics = _fetch_metrics(server=args.server, token=args.token)
            metric_counter = 0

        text = _render_live_text(state, metrics, failed_only)
        stdscr.erase()
        rows, cols = stdscr.getmaxyx()
        for idx, line in enumerate(text.splitlines()):
            if idx >= rows - 1:
                break
            stdscr.addnstr(idx, 0, line, cols - 1)
        stdscr.refresh()
        time.sleep(refresh_secs)


def _cmd_live(args: argparse.Namespace) -> int:
    if args.refresh_ms <= 0:
        print("--refresh-ms must be > 0", file=sys.stderr)
        return 1

    try:
        return curses.wrapper(lambda stdscr: _run_live_curses(stdscr, args))
    except Exception:
        state = {
            "last_event_id": 0,
            "requests": [],
            "scenario": [],
            "proxy": [],
            "health": {},
        }
        metrics: dict[str, Any] = {}
        refresh_secs = max(0.05, args.refresh_ms / 1000)
        failed_only = bool(args.failed_only)
        metric_counter = 0

        try:
            while True:
                status, payload = _fetch_live_snapshot(
                    server=args.server,
                    token=args.token,
                    since_id=int(state["last_event_id"]),
                )
                if status == 200:
                    _reduce_events(
                        state,
                        events=list(payload.get("events", [])),
                        failed_only=failed_only,
                    )
                metric_counter += 1
                if metric_counter >= max(1, int(1000 / max(1, args.refresh_ms))):
                    metrics = _fetch_metrics(server=args.server, token=args.token)
                    metric_counter = 0

                print("\x1b[2J\x1b[H" + _render_live_text(state, metrics, failed_only))
                time.sleep(refresh_secs)
        except KeyboardInterrupt:
            return 0


def _cmd_proxy(args: argparse.Namespace) -> int:
    headers_payload: dict[str, str] = {}
    if args.headers_json:
        try:
            parsed = json.loads(args.headers_json)
        except json.JSONDecodeError:
            print("--headers-json must be valid JSON", file=sys.stderr)
            return 2
        if not isinstance(parsed, dict):
            print("--headers-json must be a JSON object", file=sys.stderr)
            return 2
        headers_payload = {str(k): str(v) for k, v in parsed.items()}

    payload = {
        "target_id": args.target_id,
        "method": args.method.upper(),
        "path": args.path,
        "headers": headers_payload,
        "body": args.body or "",
    }

    status, _headers, response_payload = _request_json(
        method="POST",
        url=f"{args.server.rstrip('/')}/api/proxy/request",
        payload=payload,
        token=args.token,
    )
    if args.json:
        print(json.dumps(response_payload, indent=2, sort_keys=True))
    else:
        if status != 200:
            print(json.dumps(response_payload, indent=2, sort_keys=True), file=sys.stderr)
            return 1
        response = response_payload.get("response", {})
        print(
            "Proxy response | "
            f"target={args.target_id} "
            f"status={response.get('status')} "
            f"trace={response_payload.get('trace_id', '-') }"
        )
        print("Headers:")
        print(json.dumps(response.get("headers", {}), indent=2, sort_keys=True))
        print("Body:")
        print(str(response.get("body", "")))

    if status != 200:
        return 1
    response_status = int(response_payload.get("response", {}).get("status", 0) or 0)
    return 0 if response_status < 400 else 3


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scenario Runner CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run scenario from local JSON file")
    run.add_argument("file")
    run.add_argument("--base-url", default="https://127.0.0.1:8443")
    run.add_argument("--seed", type=int)
    run.add_argument("--json", action="store_true")
    run.add_argument("--max-steps", type=int, default=50)
    run.add_argument("--max-assertions", type=int, default=20)
    run.add_argument("--token", default="")
    run.add_argument("--live", action="store_true")

    list_cmd = sub.add_parser("list", help="List scenarios from server")
    list_cmd.add_argument("--server", default="https://127.0.0.1:8443")
    list_cmd.add_argument("--token", default="")
    list_cmd.add_argument("--json", action="store_true")

    run_remote = sub.add_parser("run-remote", help="Run saved scenario by id via server")
    run_remote.add_argument("scenario_id")
    run_remote.add_argument("--server", default="https://127.0.0.1:8443")
    run_remote.add_argument("--token", default="")
    run_remote.add_argument("--seed", type=int)
    run_remote.add_argument("--json", action="store_true")

    live = sub.add_parser("live", help="Live Ops dashboard from server event stream")
    live.add_argument("--server", default="https://127.0.0.1:8443")
    live.add_argument("--token", default="")
    live.add_argument("--refresh-ms", type=int, default=CLI_REFRESH_MS_DEFAULT)
    live.add_argument("--failed-only", action="store_true")
    live.add_argument("--no-color", action="store_true")

    proxy = sub.add_parser("proxy", help="Send one proxy request via a named target")
    proxy.add_argument("--server", default="https://127.0.0.1:8443")
    proxy.add_argument("--token", default="")
    proxy.add_argument("--target-id", required=True)
    proxy.add_argument("--method", required=True)
    proxy.add_argument("--path", required=True)
    proxy.add_argument("--body", default="")
    proxy.add_argument("--headers-json", default="{}")
    proxy.add_argument("--json", action="store_true")

    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "list":
        return _cmd_list(args)
    if args.command == "run-remote":
        return _cmd_run_remote(args)
    if args.command == "live":
        return _cmd_live(args)
    if args.command == "proxy":
        return _cmd_proxy(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
