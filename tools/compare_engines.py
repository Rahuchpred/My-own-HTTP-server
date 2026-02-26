"""Compare threadpool and selectors engines under identical load profiles."""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
import time

from server import HTTPServer
from tools.loadgen import run_load


def _start_server(engine: str) -> tuple[HTTPServer, threading.Thread]:
    server = HTTPServer(port=0, engine=engine)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()

    deadline = time.time() + 3
    while server.port == 0 and time.time() < deadline:
        time.sleep(0.01)

    if server.port == 0:
        raise RuntimeError(f"{engine} server did not bind to a port")
    return server, thread


def _stop_server(server: HTTPServer, thread: threading.Thread) -> None:
    server.stop()
    thread.join(timeout=3)


def _run_profile(
    *,
    engine: str,
    path: str,
    concurrency: int,
    duration: float,
    timeout: float,
    keepalive: bool,
    pipeline_depth: int,
) -> dict[str, float | int | dict[str, int]]:
    server, thread = _start_server(engine)
    try:
        result = asyncio.run(
            run_load(
                host=server.host,
                port=server.port,
                path=path,
                concurrency=concurrency,
                duration_secs=duration,
                timeout_secs=timeout,
                keepalive=keepalive,
                pipeline_depth=pipeline_depth,
            )
        )
    finally:
        _stop_server(server, thread)
    return result.summary()


def _gate_results(
    *,
    threadpool: dict[str, float | int | dict[str, int]],
    selectors: dict[str, float | int | dict[str, int]],
) -> dict[str, object]:
    threadpool_rps = float(threadpool["rps"])
    selectors_rps = float(selectors["rps"])
    ratio = selectors_rps / threadpool_rps if threadpool_rps > 0 else 0.0

    checks = {
        "selectors_error_rate_le_1pct": float(selectors["error_rate"]) <= 0.01,
        "selectors_p95_le_200ms": float(selectors["p95_ms"]) <= 200.0,
        "selectors_rps_ge_1_8x_threadpool": ratio >= 1.8,
    }
    return {
        "checks": checks,
        "selectors_vs_threadpool_rps_ratio": round(ratio, 4),
        "all_passed": all(checks.values()),
    }


def _markdown_table(results: dict[str, dict[str, float | int | dict[str, int]]]) -> str:
    lines = [
        "| Engine | Requests | Errors | Error Rate | RPS | p50 ms | p95 ms |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for engine in ("threadpool", "selectors"):
        summary = results[engine]
        lines.append(
            (
                "| {engine} | {requests} | {errors} | {error_rate} | "
                "{rps} | {p50_ms} | {p95_ms} |"
            ).format(
                engine=engine,
                requests=summary["requests"],
                errors=summary["errors"],
                error_rate=summary["error_rate"],
                rps=summary["rps"],
                p50_ms=summary["p50_ms"],
                p95_ms=summary["p95_ms"],
            )
        )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare HTTP server engines")
    parser.add_argument("--path", default="/")
    parser.add_argument("--concurrency", type=int, default=100)
    parser.add_argument("--duration", type=float, default=8.0)
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--keepalive", action="store_true")
    parser.add_argument("--pipeline-depth", type=int, default=1)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    results = {
        "threadpool": _run_profile(
            engine="threadpool",
            path=args.path,
            concurrency=args.concurrency,
            duration=args.duration,
            timeout=args.timeout,
            keepalive=args.keepalive,
            pipeline_depth=args.pipeline_depth,
        ),
        "selectors": _run_profile(
            engine="selectors",
            path=args.path,
            concurrency=args.concurrency,
            duration=args.duration,
            timeout=args.timeout,
            keepalive=args.keepalive,
            pipeline_depth=args.pipeline_depth,
        ),
    }

    gates = _gate_results(threadpool=results["threadpool"], selectors=results["selectors"])
    payload = {
        "results": results,
        "gates": gates,
        "markdown_table": _markdown_table(results),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
