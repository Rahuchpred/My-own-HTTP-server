#!/usr/bin/env bash
set -euo pipefail

phase="${1:-}"
if [[ -z "$phase" ]]; then
  echo "Usage: $0 P00|...|P10|V20|...|V28|V30|...|V39"
  exit 1
fi

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

if command -v python >/dev/null 2>&1; then
  py_bin="python"
elif command -v python3 >/dev/null 2>&1; then
  py_bin="python3"
else
  echo "Python interpreter not found (python/python3)."
  exit 1
fi

message=""
phase_tests=()

case "$phase" in
  P00)
    message="chore(repo): bootstrap project scaffolding and ci"
    phase_tests=("$py_bin" -m pytest -q tests/test_phase00_scaffold.py)
    ;;
  P01)
    message="feat(server): start tcp listener and accept one request"
    phase_tests=("$py_bin" -m pytest -q tests/test_phase01_tcp_server.py)
    ;;
  P02)
    message="feat(request): parse request line headers and body"
    phase_tests=("$py_bin" -m pytest -q tests/test_request.py)
    ;;
  P03)
    message="feat(response): build compliant http/1.1 byte responses"
    phase_tests=("$py_bin" -m pytest -q tests/test_response.py)
    ;;
  P04)
    message="feat(routes): serve hardcoded home route"
    phase_tests=("$py_bin" -m pytest -q tests/test_phase04_home_route.py)
    ;;
  P05)
    message="feat(router): add method path router with 404 fallback"
    phase_tests=("$py_bin" -m pytest -q tests/test_router.py)
    ;;
  P06)
    message="feat(static): serve static files with traversal protection"
    phase_tests=("$py_bin" -m pytest -q tests/test_static_handler.py)
    ;;
  P07)
    message="feat(post): handle post body for submit route"
    phase_tests=("$py_bin" -m pytest -q tests/test_phase07_post_submit.py)
    ;;
  P08)
    message="feat(server): add thread per connection handling"
    phase_tests=("$py_bin" -m pytest -q tests/test_integration.py::test_concurrent_requests)
    ;;
  P09)
    message="feat(errors): return 400 404 500 and enforce limits"
    phase_tests=("$py_bin" -m pytest -q \
      tests/test_integration.py::test_malformed_request_returns_400 \
      tests/test_integration.py::test_unsupported_method_returns_405 \
      tests/test_integration.py::test_oversized_body_returns_413)
    ;;
  P10)
    message="refactor(core): clean modules docs and final polish"
    phase_tests=("$py_bin" -m pytest -q)
    ;;
  V20)
    message="feat(core): add keep-alive request loop per connection"
    phase_tests=("$py_bin" -m pytest -q tests/test_v20_keepalive.py)
    ;;
  V21)
    message="feat(pool): add bounded thread pool and accept queue"
    phase_tests=("$py_bin" -m pytest -q tests/test_v21_thread_pool.py)
    ;;
  V22)
    message="feat(request): add chunked transfer request decoding"
    phase_tests=("$py_bin" -m pytest -q tests/test_v22_chunked_request.py)
    ;;
  V23)
    message="feat(response): add chunked transfer response streaming"
    phase_tests=("$py_bin" -m pytest -q tests/test_v23_chunked_response.py)
    ;;
  V24)
    message="feat(protocol): add date server and connection headers"
    phase_tests=("$py_bin" -m pytest -q tests/test_v24_protocol_headers.py)
    ;;
  V25)
    message="feat(routes): add head behavior and method rules"
    phase_tests=("$py_bin" -m pytest -q tests/test_v25_head_and_methods.py)
    ;;
  V26)
    message="feat(metrics): add access logging and metrics endpoint"
    phase_tests=("$py_bin" -m pytest -q tests/test_v26_metrics.py)
    ;;
  V27)
    message="test(protocol): add keep-alive chunked and queue tests"
    phase_tests=("$py_bin" -m pytest -q tests/test_v27_protocol_regression.py)
    ;;
  V28)
    message="docs(v2): document protocol behavior and tuning"
    phase_tests=("$py_bin" -m pytest -q tests/test_v2_docs.py)
    ;;
  V30)
    message="feat(static): add etag last-modified and 304 responses"
    phase_tests=("$py_bin" -m pytest -q tests/test_v30_static_cache_validators.py)
    ;;
  V31)
    message="feat(load): add load generator for concurrency testing"
    phase_tests=("$py_bin" -m pytest -q tests/test_v31_loadgen.py)
    ;;
  V32)
    message="feat(dashboard): add live metrics dashboard page"
    phase_tests=("$py_bin" -m pytest -q tests/test_v32_dashboard.py)
    ;;
  V33)
    message="perf(server): tune worker and keep-alive defaults"
    phase_tests=("$py_bin" -m pytest -q tests/test_v33_perf_tuning.py)
    ;;
  V34)
    message="test(perf): add repeatable stress smoke checks"
    phase_tests=("$py_bin" -m pytest -q tests/test_v34_perf_smoke.py)
    ;;
  V35)
    message="docs(demo): add final demo runbook and commands"
    phase_tests=("$py_bin" -m pytest -q tests/test_v35_demo_runbook.py)
    ;;
  V39)
    message="refactor(core): finalize cleanup and full validation"
    phase_tests=("$py_bin" -m pytest -q)
    ;;
  *)
    echo "Unsupported phase: $phase"
    exit 1
    ;;
esac

"$py_bin" -m ruff check .
"${phase_tests[@]}"

if git diff --quiet && git diff --cached --quiet; then
  echo "No changes detected; nothing to commit."
  exit 0
fi

git add -A
if git diff --cached --quiet; then
  echo "No staged changes after git add; nothing to commit."
  exit 0
fi

git commit -m "$message"

if [[ "${SKIP_PUSH:-0}" == "1" ]]; then
  echo "SKIP_PUSH=1 set; commit created locally without push."
  exit 0
fi

current_branch="$(git rev-parse --abbrev-ref HEAD)"
git push -u origin "$current_branch"
