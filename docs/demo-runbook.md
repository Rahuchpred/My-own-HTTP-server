# Final Demo Runbook (V5.1 Live Ops)

This runbook is optimized for a 60-90 second recording focused on live operations.

## Goal

Show two synchronized views of the same server internals:
1. web Control Tower (`/playground`),
2. terminal Live Ops dashboard (`scenario_runner.py live`).

## 1) Start server

```bash
scripts/start_server.sh
```

## 2) Open web Control Tower

Open:

```text
https://127.0.0.1:8443/playground
```

Call out:
- request lab
- target manager
- scenario launcher
- live timeline panels

## 3) Start terminal live dashboard

```bash
python3 tools/scenario_runner.py live --server https://127.0.0.1:8443
```

Keyboard controls in live mode:
- `q` quit
- `f` failed-only filter
- `r` reset event cursor
- `c` clear local view

## 4) Show scenario file and run once

In another terminal:

```bash
cat examples/scenarios/user_lifecycle.json
python3 tools/scenario_runner.py run examples/scenarios/user_lifecycle.json
python3 tools/scenario_runner.py run examples/scenarios/user_lifecycle.json --seed 42
```

Call out:
- per-step assertions
- deterministic seed replay
- trace ids

## 5) Trigger proxy target activity

Create a target from `/playground` (Targets panel), then run:

```bash
python3 tools/scenario_runner.py proxy --server https://127.0.0.1:8443 --target-id <id> --method GET --path /
```

Show both screens update with `proxy.request.completed`.

## 6) Verify event + metrics APIs

```bash
curl -k -i "https://127.0.0.1:8443/api/events/snapshot?since_id=0&limit=20"
curl -k -i https://127.0.0.1:8443/api/scenarios
curl -k -i https://127.0.0.1:8443/_metrics
```

Highlight keys:
- `scenario_runs_total`
- `scenario_runs_passed`
- `scenario_runs_failed`
- `scenario_assertions_failed_total`

## 7) End with graceful shutdown

Press `Ctrl+C` in server terminal and call out drain lifecycle + final structured logs.
