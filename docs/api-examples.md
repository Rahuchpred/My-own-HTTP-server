# API Examples

## 1) Login and session token

```bash
curl -i http://127.0.0.1:8080/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"operator","password":"operator123"}'
```

Use returned `session.token` as bearer token:

```bash
TOKEN='<session-token>'
curl -i http://127.0.0.1:8080/api/auth/me -H "Authorization: Bearer $TOKEN"
```

## 2) Live mode readiness and fallback

```bash
curl -i "http://127.0.0.1:8080/api/events/live-mode?token=$TOKEN"
```

## 3) Persistent trends (24h)

```bash
curl -i "http://127.0.0.1:8080/api/metrics/trends?window=24h&route=__all__&token=$TOKEN"
```

## 4) Incident controls (operator/admin)

```bash
curl -i http://127.0.0.1:8080/api/incidents/start \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"mode":"status_spike_503","probability":0.25,"latency_ms":0,"seed":1337}'

curl -i http://127.0.0.1:8080/api/incidents/stop \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{}'
```

## 5) Scenario run (operator/admin)

```bash
curl -i http://127.0.0.1:8080/api/scenarios/<id>/run \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"seed":42}'
```

## 6) OpenAPI contract

```bash
curl -i http://127.0.0.1:8080/openapi.json
```
