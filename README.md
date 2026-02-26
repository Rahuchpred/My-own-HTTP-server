# My-own-HTTP-server

Custom HTTP/1.1 server implemented from scratch in Python using raw TCP sockets.

## Features (V5.1)

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
- Built-in API Playground at `GET /playground` (mini-Postman style request lab)
- Dynamic mock API with persistence:
  - `POST /api/mocks`, `PUT /api/mocks/{id}`, `DELETE /api/mocks/{id}`, `GET /api/mocks`
  - `GET /api/history`, `POST /api/replay/{request_id}`, `GET /api/playground/state`
- Scenario Runner with assertions, delay, and deterministic chaos:
  - scenario CRUD: `GET/POST /api/scenarios`, `GET/PUT/DELETE /api/scenarios/{id}`
  - execution: `POST /api/scenarios/{id}/run`
  - run history: `GET /api/scenarios/{id}/runs`, `GET /api/scenarios/runs/{run_id}`
  - CLI: `tools/scenario_runner.py run`, `list`, `run-remote`, `live`, `proxy`
- Live Ops event pipeline shared by CLI + web:
  - `GET /api/events/snapshot`
  - `GET /api/events/stream`
- Named target proxying (Postman-like external routing via your server):
  - `GET/POST /api/targets`, `PUT/DELETE /api/targets/{id}`
  - `POST /api/proxy/request`
- Optional demo token guard for admin/live/proxy APIs
- Event-driven playground Control Tower (live requests, scenario timeline, proxy activity)
- JSON-file state persistence (`data/server_state.json`) for mock routes and request history
- Route-level latency summaries (`p50/p95/p99`) and error-class counters
- Structured access logs with engine, connection id, request id, trace id, shutdown phase, playground actions
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
- `GET /echo/{str}` -> echo endpoint (compatibility mode)
- `GET /user-agent` -> echoes `User-Agent` header (compatibility mode)
- `GET /files/{filename}` -> serves file bytes from `--directory` (compatibility mode)
- `POST /files/{filename}` -> writes request body to file in `--directory` (compatibility mode)
- `GET /playground` -> API Playground UI
- `GET /static/*` -> serve static files
- `GET /_metrics` -> JSON metrics snapshot
- `GET /api/mocks` -> list mock routes
- `POST /api/mocks` -> create mock route
- `PUT /api/mocks/{id}` -> update mock route
- `DELETE /api/mocks/{id}` -> delete mock route
- `GET /api/history` -> list captured request history
- `POST /api/replay/{request_id}` -> replay a captured request
- `GET /api/playground/state` -> current playground state snapshot
- `GET /api/scenarios` -> list scenarios
- `POST /api/scenarios` -> create scenario
- `GET /api/scenarios/{id}` -> fetch scenario
- `PUT /api/scenarios/{id}` -> update scenario
- `DELETE /api/scenarios/{id}` -> delete scenario
- `POST /api/scenarios/{id}/run` -> execute scenario
- `GET /api/scenarios/{id}/runs` -> list runs for scenario
- `GET /api/scenarios/runs/{run_id}` -> fetch one run report
- `GET /api/events/snapshot` -> pull event batches since cursor
- `GET /api/events/stream` -> SSE event stream
- `GET /api/targets` -> list proxy targets
- `POST /api/targets` -> create proxy target
- `PUT /api/targets/{id}` -> update target
- `DELETE /api/targets/{id}` -> delete target
- `POST /api/proxy/request` -> route request to a named target through this server
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

Fastest way (recommended):

```bash
scripts/start_server.sh
```

This starts TLS mode (`https://127.0.0.1:8443`) and auto-generates local certs if missing.

Plain HTTP mode:

```bash
scripts/start_server.sh http
```

Preview command without starting:

```bash
scripts/start_server.sh --dry-run
```

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

### CodeCrafters Compatibility Mode

Use compatibility mode to run with the CodeCrafters-style base behavior (`/echo/*`, `/user-agent`, `/files/*`):

```bash
./your_program.sh
```

With a files directory for `/files/{filename}` routes:

```bash
./your_program.sh --directory /tmp
```

You can also enable it directly:

```bash
python3 server.py --codecrafters-mode --directory /tmp
```

Notes:

- In compatibility mode, default port is `4221` unless `--port` is explicitly provided.
- Existing advanced routes/features remain available outside compatibility mode.

Server CLI options:

- `--host` (default `127.0.0.1`)
- `--port` (default `8080`, or `4221` with `--codecrafters-mode`)
- `--codecrafters-mode`
- `--directory` (used by `/files/{filename}` in compatibility mode)
- `--engine` (`threadpool` or `selectors`)
- `--max-connections`
- `--keepalive-timeout`
- `--enable-tls`, `--cert-file`, `--key-file`, `--https-port`
- `--redirect-http` / `--no-redirect-http`
- `--drain-timeout`
- `--log-format` (`plain` or `json`)
- `--enable-playground` / `--no-enable-playground`
- `--state-file` (default `data/server_state.json`)
- `--history-limit` (default `500`)
- `--max-mock-body-bytes` (default `262144`)
- `--enable-scenarios` / `--no-enable-scenarios`
- `--scenario-state-file` (default `data/scenarios_state.json`)
- `--max-scenarios` (default `100`)
- `--max-steps-per-scenario` (default `50`)
- `--max-assertions-per-step` (default `20`)
- `--default-chaos-seed` (default `1337`)
- `--enable-live-events` / `--no-enable-live-events`
- `--event-buffer-size` (default `5000`)
- `--event-heartbeat-secs` (default `5`)
- `--live-events-require-selectors` / `--no-live-events-require-selectors`
- `--cli-refresh-ms` (default `100`)
- `--enable-target-proxy` / `--no-enable-target-proxy`
- `--target-state-file` (default `data/targets_state.json`)
- `--max-targets` (default `50`)
- `--demo-token`, `--require-demo-token` / `--no-require-demo-token`
- `--public-base-url`

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
- `ENABLE_PLAYGROUND`, `STATE_FILE`, `HISTORY_LIMIT`, `MAX_MOCK_BODY_BYTES`, `MAX_MOCK_ROUTES`
- `ENABLE_SCENARIOS`, `SCENARIO_STATE_FILE`, `MAX_SCENARIOS`
- `MAX_STEPS_PER_SCENARIO`, `MAX_ASSERTIONS_PER_STEP`, `DEFAULT_CHAOS_SEED`
- `ENABLE_LIVE_EVENTS`, `EVENT_BUFFER_SIZE`, `EVENT_HEARTBEAT_SECS`, `CLI_REFRESH_MS_DEFAULT`
- `ENABLE_TARGET_PROXY`, `TARGET_STATE_FILE`, `MAX_TARGETS`
- `DEMO_TOKEN`, `REQUIRE_DEMO_TOKEN`, `PUBLIC_BASE_URL`
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

Playground quick check:

```bash
curl -k -i https://127.0.0.1:8443/playground
curl -k -i https://127.0.0.1:8443/api/playground/state
curl -k -i -X POST https://127.0.0.1:8443/api/mocks \
  -H 'Content-Type: application/json' \
  -d '{"method":"POST","path_pattern":"/api/users","status":201,"headers":{"X-Mock-Server":"custom"},"body":"{\"created\":true}","content_type":"application/json"}'
curl -k -i -X POST https://127.0.0.1:8443/api/users
curl -k -i https://127.0.0.1:8443/api/history
```

Scenario Runner quick check:

```bash
python3 tools/scenario_runner.py run examples/scenarios/user_lifecycle.json
python3 tools/scenario_runner.py run examples/scenarios/user_lifecycle.json --seed 42
python3 tools/scenario_runner.py list --server https://127.0.0.1:8443
python3 tools/scenario_runner.py live --server https://127.0.0.1:8443
python3 tools/scenario_runner.py proxy --server https://127.0.0.1:8443 --target-id <id> --method GET --path /
```

Live Ops quick check:

```bash
curl -k -i "https://127.0.0.1:8443/api/events/snapshot?since_id=0&limit=20"
curl -k -i https://127.0.0.1:8443/api/events/stream
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
