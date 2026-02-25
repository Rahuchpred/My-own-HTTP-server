# My-own-HTTP-server

Custom HTTP/1.1 server implemented from scratch in Python using raw TCP sockets.

## Requirements

- Python 3.11+
- Dev tools from `requirements-dev.txt`

## Quick Start

```bash
python -m pip install -r requirements-dev.txt
python server.py
```

## Quality Checks

```bash
ruff check .
pytest -q
```

## Phase Auto-Commits

Use the helper script to run phase checks, commit with fixed Conventional Commit messages,
and push the active branch:

```bash
scripts/autocommit_phase.sh P00
scripts/autocommit_phase.sh P01
# ...
scripts/autocommit_phase.sh P10
```

Set `SKIP_PUSH=1` to create local commits only.
