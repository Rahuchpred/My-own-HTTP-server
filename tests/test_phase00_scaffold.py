"""Sanity checks for baseline repository scaffolding."""

from pathlib import Path

from config import DEBUG, HOST, PORT

ROOT = Path(__file__).resolve().parent.parent



def test_core_files_exist() -> None:
    expected = [
        "server.py",
        "socket_handler.py",
        "request.py",
        "response.py",
        "router.py",
        "config.py",
        "utils.py",
        "handlers/example_handlers.py",
    ]
    for rel_path in expected:
        assert (ROOT / rel_path).exists()



def test_basic_config_values() -> None:
    assert HOST == "127.0.0.1"
    assert PORT == 8080
    assert DEBUG is True
