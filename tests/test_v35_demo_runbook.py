"""V35 tests for final demo runbook documentation."""

from pathlib import Path


def test_demo_runbook_contains_required_demo_steps() -> None:
    runbook_path = Path(__file__).resolve().parent.parent / "docs" / "demo-runbook.md"
    content = runbook_path.read_text(encoding="utf-8")

    required_phrases = [
        "python3 server.py",
        "python3 tools/loadgen.py",
        "https://127.0.0.1:8443/static/dashboard.html",
        "Step 7: Graceful drain demo",
    ]
    for phrase in required_phrases:
        assert phrase in content
