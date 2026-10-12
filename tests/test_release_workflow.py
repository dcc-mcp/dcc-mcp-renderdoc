"""The release PR token must trigger CI and fail closed when unavailable."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/release.yml"
TOKEN = "${{ secrets.PERSONAL_ACCESS_TOKEN }}"


def _preflight() -> str:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    before_release = workflow.split("uses: googleapis/release-please-action@", 1)[0]
    match = re.search(
        r"(?m)^      - name: Require organization release token\n"
        r"(?P<step>(?:^        .*\n|^\n)+)",
        before_release,
    )
    assert match, "The credential preflight must run before release-please"
    step = match.group("step")
    assert "        shell: bash\n" in step
    assert f"          PERSONAL_ACCESS_TOKEN: {TOKEN}\n" in step
    assert not re.search(r"(?m)^        (?:if|continue-on-error):", step)
    script = step.split("        run: |\n", 1)[1]
    return "\n".join(line[10:] for line in script.splitlines())


def _bash() -> str:
    if os.name == "nt":
        # Use Git Bash, not Windows' bash.exe launcher for an unconfigured WSL.
        git = shutil.which("git")
        assert git, "Git for Windows is required to test the bash preflight"
        for parent in Path(git).resolve().parents:
            candidate = parent / "bin" / "bash.exe"
            if candidate.is_file():
                return str(candidate)
        pytest.fail("Cannot find Git for Windows' bin/bash.exe")
    bash = shutil.which("bash")
    assert bash, "Bash is required to test the release workflow"
    return bash


def test_release_please_uses_only_the_organization_token() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    action = workflow.split("uses: googleapis/release-please-action@", 1)[1]
    assert re.search(rf"(?m)^          token: {re.escape(TOKEN)}$", action)
    assert "github.token" not in workflow
    assert "secrets.GITHUB_TOKEN" not in workflow
    _preflight()


@pytest.mark.parametrize("token", [None, "", "synthetic-token-do-not-print"])
def test_release_token_preflight_fails_closed_without_leaking(token: str | None) -> None:
    env = os.environ.copy()
    env.pop("PERSONAL_ACCESS_TOKEN", None)
    if token is not None:
        env["PERSONAL_ACCESS_TOKEN"] = token
    result = subprocess.run(
        [_bash(), "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", _preflight()],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if token:
        assert result.returncode == 0, result.stderr
        assert token not in result.stdout + result.stderr
    else:
        assert result.returncode != 0
        assert "::error::" in result.stdout + result.stderr
        assert "PERSONAL_ACCESS_TOKEN" in result.stdout + result.stderr
