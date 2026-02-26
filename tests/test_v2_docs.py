"""V28 docs checks for V3 protocol behavior and tuning guidance."""

from pathlib import Path


def test_readme_contains_v3_protocol_sections() -> None:
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")

    required_sections = [
        "Features (V3)",
        "Persistent HTTP/1.1 keep-alive connections",
        "Chunked request body decoding",
        "HTTPS/TLS listener with secure defaults",
        "GET /_metrics",
        "Tuning Knobs",
        "Troubleshooting",
        "Phase Auto-Commits",
    ]
    for section in required_sections:
        assert section in readme
