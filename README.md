# My-own-HTTP-server

Custom HTTP/1.1 server implemented from scratch in Python using raw TCP sockets.

## Features (V2)

- Raw TCP socket server (`AF_INET`, `SOCK_STREAM`)
- Persistent HTTP/1.1 keep-alive connections with per-socket request loop
- Bounded worker-pool concurrency with request queue backpressure (`503` on saturation)
- Manual HTTP request parsing (`GET`, `POST`, `HEAD`)
- Chunked request body decoding (`Transfer-Encoding: chunked`)
- Manual HTTP response serialization with optional chunked streaming
- Auto protocol headers: `Date`, `Server`, `Connection`, `Keep-Alive`
- Method/path routing with `405` allow headers
- Static file serving from `./static` with traversal protection
- Built-in metrics endpoint: `GET /_metrics`
- Structured access logs (client, method, path, status, bytes, latency, reuse flag)
- Basic safeguards: header/body/request limits and socket timeouts

## Routes

- `GET /` -> Hello world text response
- `GET /stream` -> chunked demo response
- `POST /submit` -> echoes submitted body
- `GET /static/*` -> serve static files
- `GET /_metrics` -> JSON metrics snapshot
- `HEAD` supported for routed/static/metrics endpoints

## Project Structure

- `server.py`: accept loop, worker pool integration, dispatch, connection lifecycle
- `thread_pool.py`: fixed worker pool and bounded queue
- `socket_handler.py`: framed request reading (content-length + chunked)
- `request.py`: `HTTPRequest` model and parser
- `response.py`: `HTTPResponse` model and serializer (fixed + chunked)
- `metrics.py`: in-memory counters/histograms
- `router.py`: route registry + lookup
- `handlers/example_handlers.py`: route handlers
- `utils.py`: static path + MIME helpers
- `config.py`: runtime constants and tuning knobs
- `tests/`: unit + integration + phased validation tests

## Requirements

- Python 3.11+
- Dev dependencies from `requirements-dev.txt`

## Setup

```bash
python3 -m pip install -r requirements-dev.txt
```

## Run Server

```bash
python3 server.py
```

Server defaults:

- host: `127.0.0.1`
- port: `8080`

## Tuning Knobs

Defined in `config.py`:

- `WORKER_COUNT`: fixed worker threads handling accepted sockets
- `REQUEST_QUEUE_SIZE`: max queued accepted sockets
- `KEEPALIVE_TIMEOUT_SECS`: idle timeout per keep-alive connection
- `MAX_KEEPALIVE_REQUESTS`: max requests served per connection
- `MAX_HEADER_BYTES`, `MAX_BODY_BYTES`, `MAX_REQUEST_BYTES`: safety limits
- `SERVER_NAME`: default `Server` response header value

## Manual Checks

```bash
curl -i http://127.0.0.1:8080/
curl -i http://127.0.0.1:8080/stream
curl -i -X POST http://127.0.0.1:8080/submit -d "name=test"
curl -i http://127.0.0.1:8080/static/test.html
curl -i http://127.0.0.1:8080/_metrics
curl -i -X HEAD http://127.0.0.1:8080/
```

## Automated Checks

```bash
python3 -m ruff check .
python3 -m pytest -q
```

## Troubleshooting

- Keep-alive client hangs:
  - ensure client sends full HTTP request framing (`\r\n\r\n`, correct chunk/content-length)
  - verify `Connection: close` is sent when you expect one-shot behavior
- Chunked request rejected (`400`):
  - check hex chunk sizes, chunk CRLF separators, and terminal `0\r\n\r\n`
  - do not send `Content-Length` together with `Transfer-Encoding: chunked`
- Frequent `503 Service Unavailable` under load:
  - increase `WORKER_COUNT` and `REQUEST_QUEUE_SIZE`
  - reduce per-request blocking work in handlers
- Unexpected `413 Payload Too Large`:
  - compare client payload size to `MAX_BODY_BYTES` and `MAX_REQUEST_BYTES`

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
