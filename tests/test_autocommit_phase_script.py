"""Behavioral tests for scripts/autocommit_phase.sh."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_SOURCE = PROJECT_ROOT / "scripts" / "autocommit_phase.sh"


def _run(
    command: list[str],
    cwd: Path,
    *,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"Command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def _commit_count(repo_path: Path) -> int:
    output = _run(["git", "rev-list", "--count", "HEAD"], cwd=repo_path).stdout.strip()
    return int(output)


def _init_repo(
    tmp_path: Path,
    *,
    include_v20_test: bool = False,
    with_remote: bool = False,
) -> Path:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    (repo_path / "scripts").mkdir()
    (repo_path / "tests").mkdir()

    shutil.copy2(SCRIPT_SOURCE, repo_path / "scripts" / "autocommit_phase.sh")
    os.chmod(
        repo_path / "scripts" / "autocommit_phase.sh",
        stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR,
    )

    (repo_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts = \"-q\"\n",
        encoding="utf-8",
    )
    (repo_path / "app.py").write_text(
        "def add(a: int, b: int) -> int:\n"
        "    return a + b\n",
        encoding="utf-8",
    )
    (repo_path / "tests" / "test_smoke.py").write_text(
        "def test_smoke() -> None:\n"
        "    assert 1 + 1 == 2\n",
        encoding="utf-8",
    )

    if include_v20_test:
        (repo_path / "tests" / "test_v20_keepalive.py").write_text(
            "def test_v20_keepalive_placeholder() -> None:\n"
            "    assert True\n",
            encoding="utf-8",
        )

    (repo_path / "notes.txt").write_text("baseline\n", encoding="utf-8")

    _run(["git", "init", "-b", "main"], cwd=repo_path)
    _run(["git", "config", "user.email", "test@example.com"], cwd=repo_path)
    _run(["git", "config", "user.name", "Test User"], cwd=repo_path)

    if with_remote:
        remote_path = tmp_path / "remote.git"
        _run(["git", "init", "--bare", str(remote_path)], cwd=tmp_path)
        _run(["git", "remote", "add", "origin", str(remote_path)], cwd=repo_path)

    _run(["git", "add", "-A"], cwd=repo_path)
    _run(["git", "commit", "-m", "init"], cwd=repo_path)

    return repo_path


def _run_autocommit(
    repo_path: Path,
    phase: str,
    *,
    skip_push: bool,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if skip_push:
        env["SKIP_PUSH"] = "1"
    return _run(
        ["bash", "scripts/autocommit_phase.sh", phase],
        cwd=repo_path,
        check=False,
        env=env,
    )


def test_unknown_phase_exits_nonzero_without_commit(tmp_path: Path) -> None:
    repo_path = _init_repo(tmp_path)
    before = _commit_count(repo_path)

    result = _run_autocommit(repo_path, "UNKNOWN", skip_push=True)

    assert result.returncode != 0
    assert "Unsupported phase: UNKNOWN" in f"{result.stdout}\n{result.stderr}"
    assert _commit_count(repo_path) == before


def test_gate_failure_exits_nonzero_without_commit(tmp_path: Path) -> None:
    repo_path = _init_repo(tmp_path)
    (repo_path / "bad.py").write_text("import os\n", encoding="utf-8")
    before = _commit_count(repo_path)

    result = _run_autocommit(repo_path, "P10", skip_push=True)

    assert result.returncode != 0
    assert _commit_count(repo_path) == before


def test_no_changes_exits_cleanly_without_commit(tmp_path: Path) -> None:
    repo_path = _init_repo(tmp_path)
    before = _commit_count(repo_path)

    result = _run_autocommit(repo_path, "P10", skip_push=True)

    assert result.returncode == 0
    assert "No changes detected; nothing to commit." in f"{result.stdout}\n{result.stderr}"
    assert _commit_count(repo_path) == before


def test_successful_v20_phase_creates_expected_commit_message(tmp_path: Path) -> None:
    repo_path = _init_repo(tmp_path, include_v20_test=True)
    (repo_path / "notes.txt").write_text("baseline\nv20 change\n", encoding="utf-8")
    before = _commit_count(repo_path)

    result = _run_autocommit(repo_path, "V20", skip_push=True)

    assert result.returncode == 0
    assert _commit_count(repo_path) == before + 1
    commit_subject = _run(["git", "log", "-1", "--pretty=%s"], cwd=repo_path).stdout.strip()
    assert commit_subject == "feat(core): add keep-alive request loop per connection"


def test_successful_phase_pushes_to_current_branch(tmp_path: Path) -> None:
    repo_path = _init_repo(tmp_path, with_remote=True)
    (repo_path / "notes.txt").write_text("baseline\npush me\n", encoding="utf-8")

    result = _run_autocommit(repo_path, "P10", skip_push=False)
    assert result.returncode == 0

    current_branch = _run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_path,
    ).stdout.strip()
    assert current_branch == "main"

    local_head = _run(["git", "rev-parse", "HEAD"], cwd=repo_path).stdout.strip()
    remote_heads = _run(
        ["git", "ls-remote", "--heads", "origin", "main"],
        cwd=repo_path,
    ).stdout.strip()
    assert remote_heads
    remote_head = remote_heads.split()[0]
    assert remote_head == local_head
