#!/usr/bin/env bash
set -euo pipefail

phase="${1:-}"
if [[ -z "$phase" ]]; then
  echo "Usage: $0 P00|P01|...|P10"
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
