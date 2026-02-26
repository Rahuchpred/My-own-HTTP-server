"""Docs checks for V5 scenario engine and playground guidance."""

from pathlib import Path


def test_readme_contains_v5_protocol_sections() -> None:
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")

    required_sections = [
        "Features (V5.1)",
        "Persistent HTTP/1.1 keep-alive connections",
        "Chunked request body decoding",
        "HTTPS/TLS listener with secure defaults",
        "Built-in API Playground at `GET /playground`",
        "Scenario Runner",
        "GET /_metrics",
        "Tuning Knobs",
        "Troubleshooting",
        "Phase Auto-Commits",
    ]
    for section in required_sections:
        assert section in readme
