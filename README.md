# My-own-HTTP-server

Custom HTTP/1.1 server implemented from scratch in Python using raw TCP sockets.

## Features (V3)

- Raw TCP socket server (`AF_INET`, `SOCK_STREAM`)
- HTTPS/TLS listener with secure defaults (TLS 1.2+)
- Optional HTTP -> HTTPS redirect listener (`308 Permanent Redirect`)
- Dual runtime engines:
  - `threadpool` engine (bounded worker pool)
  - `selectors` engine (non-blocking event loop)
- Persistent HTTP/1.1 keep-alive connections
- Pipelined request handling in selectors engine with ordered responses
- Chunked request body decoding (`Transfer-Encoding: chunked`)
- Incremental chunked response streaming (no pre-buffered stream payload)
- Incremental static-file delivery (`os.sendfile` when available, read-chunk fallback)
- Protocol hardening:
  - HTTP/1.1 Host header enforcement
  - HTTP version checks (`505` for unsupported versions)
  - request-target length checks (`414`)
  - header-size checks (`431`)
  - unknown method handling (`501`)
- `Expect: 100-continue` support
- Auto protocol headers: `Date`, `Server`, `Connection`, `Keep-Alive`
- Static cache validators (`ETag`, `Last-Modified`, `304 Not Modified`)
- Graceful shutdown + connection draining with `Retry-After` rejection for post-drain requests
- Built-in metrics endpoint: `GET /_metrics`
- Route-level latency summaries (`p50/p95/p99`) and error-class counters
- Structured access logs with engine, connection id, request id, trace id, shutdown phase
- Load-testing and benchmark tooling (`tools/loadgen.py`, `tools/compare_engines.py`)

## Architecture

```text
                +----------------------+
TCP accept ----> | Engine Selector     | ----> threadpool engine
                | (threadpool/selectors)|
                +----------+-----------+
                           |
                           +---------> selectors event loop
                                        |  read buffer
                                        |  parse/framing
                                        |  dispatch/router
                                        |  queued ordered writes
                                        +--> socket send (body/stream/file)
```

## Protocol Matrix

| Behavior | Implemented |
| --- | --- |
| HTTP/1.1 + HTTP/1.0 parsing | Yes |
| Missing `Host` on HTTP/1.1 | `400 Bad Request` |
| Unsupported version | `505 HTTP Version Not Supported` |
| Unknown method token | `501 Not Implemented` |
| Known but disallowed method | `405 Method Not Allowed` |
| Oversized request target | `414 URI Too Long` |
| Oversized headers | `431 Request Header Fields Too Large` |
| Oversized body | `413 Payload Too Large` |
| Keep-alive semantics | Yes |
| Chunked request body decode | Yes |
| Chunked response stream encode | Yes |
| `Expect: 100-continue` | Yes |
| HTTPS/TLS server mode | Yes |
| HTTP -> HTTPS redirect | Yes |

## Routes

- `GET /` -> Hello world text response
- `GET /stream` -> chunked demo response
- `POST /submit` -> echoes submitted body
- `GET /static/*` -> serve static files
- `GET /_metrics` -> JSON metrics snapshot
- `HEAD` supported for routed/static/metrics endpoints

## Project Structure

- `server.py`: engine orchestration, lifecycle, dispatch, logging
- `thread_pool.py`: fixed worker pool and bounded queue
- `socket_handler.py`: framing/extraction utilities and incremental response writer
- `request.py`: `HTTPRequest` model and strict parser
- `response.py`: `HTTPResponse` model and serializer/preparer
- `metrics.py`: in-memory metrics counters and latency buckets
- `router.py`: route registry + lookup
- `handlers/example_handlers.py`: route handlers
- `utils.py`: static path + MIME helpers
- `config.py`: runtime constants and tuning knobs
- `tools/loadgen.py`: async load generator (keep-alive + pipeline options)
- `tools/compare_engines.py`: benchmark and gate checker
- `tests/`: unit + integration + phase validation tests

## Requirements

- Python 3.11+
- Dev dependencies from `requirements-dev.txt`

## Setup

```bash
python3 -m pip install -r requirements-dev.txt
tools/generate_dev_cert.sh certs
```

## Run Server

Default (threadpool engine):

```bash
python3 server.py
```

Selectors engine with JSON logs:

```bash
python3 server.py --engine selectors --log-format json
```

TLS mode with redirect:

```bash
python3 server.py --enable-tls --cert-file certs/dev-cert.pem --key-file certs/dev-key.pem --https-port 8443 --redirect-http
```

Server CLI options:

- `--host` (default `127.0.0.1`)
- `--port` (default `8080`)
- `--engine` (`threadpool` or `selectors`)
- `--max-connections`
- `--keepalive-timeout`
- `--enable-tls`, `--cert-file`, `--key-file`, `--https-port`
- `--redirect-http` / `--no-redirect-http`
- `--drain-timeout`
- `--log-format` (`plain` or `json`)

## Tuning Knobs

Defined in `config.py`:

- `SERVER_ENGINE`
- `WORKER_COUNT`, `REQUEST_QUEUE_SIZE`
- `MAX_ACTIVE_CONNECTIONS`
- `KEEPALIVE_TIMEOUT_SECS`, `MAX_KEEPALIVE_REQUESTS`
- `SELECT_TIMEOUT_SECS`, `IDLE_SWEEP_INTERVAL_SECS`
- `DRAIN_TIMEOUT_SECS`, `SHUTDOWN_POLL_INTERVAL_SECS`
- `READ_CHUNK_SIZE`, `WRITE_CHUNK_SIZE`
- `MAX_HEADER_BYTES`, `MAX_BODY_BYTES`, `MAX_REQUEST_BYTES`, `MAX_TARGET_LENGTH`
- `ENABLE_EXPECT_CONTINUE`
- `ENABLE_TLS`, `TLS_CERT_FILE`, `TLS_KEY_FILE`, `HTTPS_PORT`, `REDIRECT_HTTP_TO_HTTPS`
- `SERVER_NAME`

## Manual Checks

```bash
curl -i http://127.0.0.1:8080/
curl -i http://127.0.0.1:8080/stream
curl -i -X POST http://127.0.0.1:8080/submit -d "name=test"
curl -i http://127.0.0.1:8080/static/test.html
curl -i http://127.0.0.1:8080/_metrics
curl -i -X HEAD http://127.0.0.1:8080/
```

Expect/continue quick check:

```bash
printf 'POST /submit HTTP/1.1\r\nHost: localhost\r\nExpect: 100-continue\r\nContent-Length: 4\r\nConnection: close\r\n\r\n' | nc 127.0.0.1 8080
```

TLS quick check:

```bash
curl -k -i https://127.0.0.1:8443/
curl -i http://127.0.0.1:8080/
```

## Performance Benchmarking

Keep-alive + pipelined load:

```bash
python3 tools/loadgen.py --host 127.0.0.1 --port 8080 --path / --concurrency 200 --duration 20 --keepalive --pipeline-depth 4
```

Compare both engines and evaluate gates:

```bash
python3 tools/compare_engines.py --path / --concurrency 200 --duration 10 --keepalive --pipeline-depth 4
```

Output includes:

- per-engine summary (`requests`, `error_rate`, `rps`, `p50`, `p95`)
- markdown table for demo screenshots
- gate checks:
  - selectors error rate `<= 1%`
  - selectors p95 `<= 200ms`
  - selectors RPS `>= 1.8x` threadpool

## Automated Checks

```bash
python3 -m ruff check .
python3 -m pytest -q
```

## Troubleshooting

- Keep-alive client hangs:
  - ensure client sends full HTTP framing (`\r\n\r\n`, correct content-length/chunks)
  - verify `Connection: close` if one-shot behavior is expected
- `100 Continue` not observed:
  - send headers first with `Expect: 100-continue`
  - do not send body bytes before waiting for interim response
- TLS startup fails:
  - verify cert/key files exist and are readable
  - regenerate local certs with `tools/generate_dev_cert.sh certs`
- Frequent `503 Service Unavailable` under load:
  - increase `WORKER_COUNT` and `REQUEST_QUEUE_SIZE` for threadpool runs
  - lower concurrency or switch to `--engine selectors`
- Unexpected `413`/`431`/`414`:
  - compare request size and target length against limits in `config.py`

## Phase Auto-Commits

Use the helper script to run phase-specific checks, then commit with fixed Conventional Commit messages.

```bash
scripts/autocommit_phase.sh P00
scripts/autocommit_phase.sh P01
scripts/autocommit_phase.sh P10
scripts/autocommit_phase.sh V20
# ...
scripts/autocommit_phase.sh V39
```

By default, the script pushes the active branch after a successful commit.
Set `SKIP_PUSH=1` to skip push and keep local-only commits.

## Demo Runbook

For final showcase steps (server start, load profile, dashboard flow), use:

- `docs/demo-runbook.md`
