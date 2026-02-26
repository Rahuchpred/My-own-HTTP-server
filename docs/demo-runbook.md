# Final Demo Runbook

This runbook is optimized for a high-impact recording that proves the server is built from low-level primitives, not framework glue.

## Goal

Show protocol-level behavior, non-blocking engine architecture, and measurable performance gates in one cohesive walkthrough.

## Prerequisites

- Python 3.11+
- Dependencies installed (`python3 -m pip install -r requirements-dev.txt`)
- Free local port 8080 (or pass `--port`)

## Step 1: Start the selectors engine

```bash
python3 server.py --engine selectors --log-format json
```

Keep this terminal visible for structured logs (`engine`, `connection_id`, `request_id`, `latency_ms`).

## Step 2: Open the live dashboard

In a browser, open:

```text
http://127.0.0.1:8080/static/dashboard.html
```

You should see counters updating every second via `GET /_metrics`.

## Step 3: Prove strict protocol behavior

Run these commands in another terminal:

```bash
# Missing Host in HTTP/1.1 -> 400
printf 'GET / HTTP/1.1\r\nConnection: close\r\n\r\n' | nc 127.0.0.1 8080

# Unsupported version -> 505
printf 'GET / HTTP/2.0\r\nHost: localhost\r\nConnection: close\r\n\r\n' | nc 127.0.0.1 8080
```

Call out that these are parser-level checks, not route-level behavior.

## Step 4: Show interim `100 Continue`

Send headers first:

```bash
printf 'POST /submit HTTP/1.1\r\nHost: localhost\r\nExpect: 100-continue\r\nContent-Length: 4\r\nConnection: close\r\n\r\n' | nc 127.0.0.1 8080
```

You should see `HTTP/1.1 100 Continue` before the final response in compliant client flows.

## Step 5: Show pipelined ordering

Use this one-shot Python snippet:

```bash
python3 - <<'PY'
import socket

payload = (
    b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: keep-alive\r\n\r\n"
    b"GET /missing HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
)
with socket.create_connection(("127.0.0.1", 8080), timeout=2) as sock:
    sock.sendall(payload)
    print(sock.recv(4096).decode("iso-8859-1", errors="replace"))
    print(sock.recv(4096).decode("iso-8859-1", errors="replace"))
PY
```

Explain that responses stay ordered under pipelined input.

## Step 6: Run stress profile with keep-alive + pipeline

```bash
python3 tools/loadgen.py --host 127.0.0.1 --port 8080 --path / --concurrency 200 --duration 20 --keepalive --pipeline-depth 4
```

Then run a heavier profile if needed:

```bash
python3 tools/loadgen.py --host 127.0.0.1 --port 8080 --path / --concurrency 500 --duration 30 --keepalive --pipeline-depth 4
```

## Step 7: Compare engines with hard gates

```bash
python3 tools/compare_engines.py --path / --concurrency 200 --duration 10 --keepalive --pipeline-depth 4
```

Use the output to show:

- threadpool vs selectors throughput and latency
- strict gate checks:
  - selectors error rate `<= 1%`
  - selectors p95 `<= 200ms`
  - selectors RPS `>= 1.8x` threadpool

## What to call out during demo

- The request parser returns protocol-specific statuses (`400`, `414`, `431`, `501`, `505`).
- `Expect: 100-continue` and pipelined response ordering are implemented manually.
- Static files are sent incrementally (not fully buffered).
- `/_metrics` and dashboard move in real time under load.
- Engine comparison is repeatable and gate-based, not anecdotal.

## Quick health commands

```bash
curl -i http://127.0.0.1:8080/
curl -i http://127.0.0.1:8080/_metrics
curl -i http://127.0.0.1:8080/static/dashboard.html
```

## Legacy baseline run (optional)

To compare legacy concurrency behavior:

```bash
python3 server.py
```
