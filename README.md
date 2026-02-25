# My-own-HTTP-server

Custom HTTP/1.1 server implemented from scratch in Python using raw TCP sockets.

## Features

- Raw TCP socket server (`AF_INET`, `SOCK_STREAM`)
- Manual HTTP request parsing (method, path, version, headers, body)
- Manual HTTP response serialization
- Method/path router
- Static file serving from `./static`
- `POST /submit` body handling
- Thread-per-connection concurrency model
- Basic security controls (body/header limits, traversal protection, socket timeout)

## Project Structure

- `server.py`: server lifecycle, threading, dispatch
- `socket_handler.py`: low-level request read/response write
- `request.py`: `HTTPRequest` model + parser
- `response.py`: `HTTPResponse` model + serializer
- `router.py`: route registry + lookup
- `handlers/example_handlers.py`: route handlers
- `utils.py`: static path + MIME helpers
- `config.py`: runtime constants
- `tests/`: unit and integration tests

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

## Manual Checks

```bash
curl http://127.0.0.1:8080/
curl http://127.0.0.1:8080/static/test.html
curl -X POST http://127.0.0.1:8080/submit -d "name=test"
curl http://127.0.0.1:8080/unknown
```

## Automated Checks

```bash
python3 -m ruff check .
python3 -m pytest -q
```

## Phase Auto-Commits

Use the helper script to run phase-specific checks, then commit with fixed Conventional Commit messages.

```bash
scripts/autocommit_phase.sh P00
scripts/autocommit_phase.sh P01
# ...
scripts/autocommit_phase.sh P10
```

By default, the script pushes the active branch after a successful commit.
Set `SKIP_PUSH=1` to skip push and keep local-only commits.
