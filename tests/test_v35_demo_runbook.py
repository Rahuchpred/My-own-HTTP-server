"""V35 tests for final demo runbook documentation."""

from pathlib import Path


def test_demo_runbook_contains_required_demo_steps() -> None:
    runbook_path = Path(__file__).resolve().parent.parent / "docs" / "demo-runbook.md"
    content = runbook_path.read_text(encoding="utf-8")

    required_phrases = [
        "scripts/start_server.sh",
        "https://127.0.0.1:8443/playground",
        "Create Mock",
        "curl -k -i https://127.0.0.1:8443/api/history",
        "End with graceful shutdown",
    ]
    for phrase in required_phrases:
        assert phrase in content
