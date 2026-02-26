# Final Demo Runbook (V3)

This runbook is optimized for a recording that proves secure transport, graceful lifecycle handling, and deep observability on top of your from-scratch HTTP stack.

## Goal

Show TLS-first serving, HTTP redirect behavior, graceful draining semantics, and route-level latency metrics in one cohesive demo.

## Prerequisites

- Python 3.11+
- Dependencies installed (`python3 -m pip install -r requirements-dev.txt`)
- OpenSSL available (`openssl version`)

## Step 0: Generate dev certificates

```bash
tools/generate_dev_cert.sh certs
```

This creates:

- `certs/dev-cert.pem`
- `certs/dev-key.pem`

## Step 1: Start server in TLS mode

```bash
python3 server.py \
  --engine selectors \
  --enable-tls \
  --cert-file certs/dev-cert.pem \
  --key-file certs/dev-key.pem \
  --port 8080 \
  --https-port 8443 \
  --redirect-http \
  --log-format json
```

Call out in logs:

- `trace_id`
- `route_key`
- `shutdown_phase`
- `drain_elapsed_ms`

## Step 2: Prove HTTP -> HTTPS redirect

```bash
curl -i http://127.0.0.1:8080/static/test.html
```

Expected: `308 Permanent Redirect` with `Location: https://127.0.0.1:8443/static/test.html`.

## Step 3: Prove HTTPS request handling

```bash
curl -k -i https://127.0.0.1:8443/
curl -k -i https://127.0.0.1:8443/_metrics
```

Expected: normal application responses over TLS.

## Step 4: Open live dashboard over HTTPS

In a browser, open:

```text
https://127.0.0.1:8443/static/dashboard.html
```

(accept local cert warning once)

## Step 5: Show protocol strictness over TLS

```bash
printf 'GET / HTTP/2.0\r\nHost: localhost\r\nConnection: close\r\n\r\n' | openssl s_client -quiet -connect 127.0.0.1:8443
```

Call out `505 HTTP Version Not Supported`.

## Step 6: Load + observability

```bash
python3 tools/loadgen.py --host 127.0.0.1 --port 8443 --path / --concurrency 200 --duration 20 --keepalive --pipeline-depth 4
```

Then query metrics:

```bash
curl -k -s https://127.0.0.1:8443/_metrics | python3 -m json.tool
```

Highlight:

- `requests_by_route`
- `latency_by_route_ms` (`p50/p95/p99`)
- `error_counts_by_class`
- `request_trace_id`

## Step 7: Graceful drain demo

With load still running (or immediately after), stop server with `Ctrl+C`.

Call out behavior:

- server enters draining phase
- in-flight work completes
- post-drain requests get `503` + `Retry-After`
- logs include `shutdown_phase=draining` and `drain_elapsed_ms`

## Step 8: Optional engine comparison

```bash
python3 tools/compare_engines.py --path / --concurrency 200 --duration 10 --keepalive --pipeline-depth 4
```

Use this to reinforce that complexity is measurable, not just cosmetic.

## Quick health commands

```bash
curl -k -i https://127.0.0.1:8443/
curl -k -i https://127.0.0.1:8443/_metrics
curl -i http://127.0.0.1:8080/
```
