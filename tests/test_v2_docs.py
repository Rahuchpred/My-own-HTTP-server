"""Docs checks for V4 protocol behavior and playground guidance."""

from pathlib import Path


def test_readme_contains_v4_protocol_sections() -> None:
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")

    required_sections = [
        "Features (V4)",
        "Persistent HTTP/1.1 keep-alive connections",
        "Chunked request body decoding",
        "HTTPS/TLS listener with secure defaults",
        "Built-in API Playground at `GET /playground`",
        "GET /_metrics",
        "Tuning Knobs",
        "Troubleshooting",
        "Phase Auto-Commits",
    ]
    for section in required_sections:
        assert section in readme
