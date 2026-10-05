# My own HTTP server

An HTTP/1.1 server built from scratch in Python, on raw TCP sockets. No frameworks.

I built it to learn how the web works under the hood: parsing, keep-alive, TLS, streaming and graceful shutdown.

## What it does

- Two engines: a thread pool, and a non-blocking event loop with pipelining
- HTTPS with TLS 1.2+, and an HTTP to HTTPS redirect
- Chunked bodies, streaming responses, `sendfile` for static files, ETags
- Proper errors for bad requests (`400`, `413`, `414`, `431`, `501`, `505`)
- A built-in API playground at `/playground`, with mock routes, request replay and scenarios
- Metrics with p50, p95 and p99 latency at `/_metrics`

## Run

Needs Python 3.11+.

```bash
python3 -m pip install -r requirements-dev.txt
scripts/start_server.sh        # https://127.0.0.1:8443
scripts/start_server.sh http   # plain HTTP
```

Then open `/playground`.

## More

Every route, flag and benchmark is in [docs/reference.md](docs/reference.md).
