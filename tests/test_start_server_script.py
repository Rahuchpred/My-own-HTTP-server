"""Behavioral tests for scripts/start_server.sh launcher."""

from __future__ import annotations

import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "start_server.sh"


def _run_script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT_PATH), *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_help_displays_usage() -> None:
    result = _run_script("--help")

    assert result.returncode == 0
    assert "Usage: scripts/start_server.sh" in result.stdout


def test_default_mode_resolves_tls_command() -> None:
    result = _run_script("--dry-run")

    assert result.returncode == 0
    assert "Resolved command:" in result.stdout
    assert "--enable-tls" in result.stdout
    assert "--https-port" in result.stdout


def test_http_mode_resolves_without_tls_flags() -> None:
    result = _run_script("http", "--dry-run")

    assert result.returncode == 0
    assert "Resolved command:" in result.stdout
    assert "--enable-tls" not in result.stdout
    assert "--https-port" not in result.stdout


def test_custom_options_are_reflected_in_resolved_command(tmp_path: Path) -> None:
    cert_dir = tmp_path / "certs"
    result = _run_script(
        "tls",
        "--dry-run",
        "--engine",
        "threadpool",
        "--port",
        "9090",
        "--https-port",
        "9443",
        "--cert-dir",
        str(cert_dir),
        "--no-redirect",
    )

    assert result.returncode == 0
    assert "--engine threadpool" in result.stdout
    assert "--port 9090" in result.stdout
    assert "--https-port 9443" in result.stdout
    assert f"--cert-file {cert_dir}/dev-cert.pem" in result.stdout
    assert f"--key-file {cert_dir}/dev-key.pem" in result.stdout
    assert "--no-redirect-http" in result.stdout
