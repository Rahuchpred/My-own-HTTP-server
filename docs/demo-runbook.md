# Final Demo Runbook (V4)

This runbook is optimized for a 60-90 second recording that looks useful and technically deep.

## Goal

Show a real HTTPS server with a built-in API Playground that can create mocks, send requests, inspect responses, and replay traffic.

## Prerequisites

- Python 3.11+
- Dependencies installed (`python3 -m pip install -r requirements-dev.txt`)

## 1) Start server with one command

```bash
scripts/start_server.sh
```

## 2) Open the app

Open in browser:

```text
https://127.0.0.1:8443/playground
```

Call out:
- request builder (method/path/headers/body)
- mock manager
- history + replay
- trace and latency in UI/logs

## 3) Create a real mock from UI

Create mock:
- method: `POST`
- path: `/api/users`
- status: `201`
- body: `{"id":101,"name":"Demo User","source":"mock"}`

Click **Create Mock**.

## 4) Send live request from playground

In Request Lab, send:
- method: `POST`
- path: `/api/users`

Show response panel:
- status `201`
- response headers/body
- latency

## 5) Show history + replay

In history panel:
- pick latest `/api/users` entry
- click **Replay**
- show new history row appears

## 6) Prove with terminal commands

Run in second terminal:

```bash
curl -k -i https://127.0.0.1:8443/api/playground/state
curl -k -i https://127.0.0.1:8443/api/history
curl -k -i https://127.0.0.1:8443/_metrics
```

Highlight metrics keys:
- `playground_mock_count`
- `playground_history_count`
- `playground_replay_total`
- `request_trace_id`

## 7) Show HTTPS + redirect proof

```bash
curl -i http://127.0.0.1:8080/
curl -k -i https://127.0.0.1:8443/
```

Expected:
- HTTP returns `308 Permanent Redirect`
- HTTPS serves normal `200` response

## 8) End with graceful shutdown

Stop server with `Ctrl+C` in server terminal.

Call out:
- draining shutdown lifecycle
- structured logs with trace IDs and route keys
