"""Shell-level test for the committed npm ``bin/`` shims (AD3).

These are plain ``sh`` scripts, not Python, so this exercises them the way
they actually run: through a symlink (mimicking what npm creates in the
global bin dir), resolving back to the real package directory, then either
execing the real console script or self-healing a missing venv (D5).
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SHIM = REPO_ROOT / "bin" / "claudespace.sh"


def _make_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def fake_package(tmp_path):
    """A throwaway package layout: bin/claudespace.sh, a stub .venv, and a
    global-bin symlink pointing at the shim - the same shape npm's own
    reify step produces.
    """
    pkg = tmp_path / "pkg"
    (pkg / "bin").mkdir(parents=True)
    (pkg / ".venv" / "bin").mkdir(parents=True)
    (pkg / "scripts").mkdir()

    shim_copy = pkg / "bin" / "claudespace.sh"
    shim_copy.write_text(SHIM.read_text())
    shim_copy.chmod(SHIM.stat().st_mode)

    _make_executable(
        pkg / ".venv" / "bin" / "claudespace",
        '#!/bin/sh\necho "real claudespace invoked with: $*"\n',
    )

    global_bin = tmp_path / "globalbin"
    global_bin.mkdir()
    os.symlink(shim_copy, global_bin / "claudespace")

    return pkg, global_bin / "claudespace"


def test_execs_the_real_console_script_when_venv_is_present(fake_package):
    pkg, entry = fake_package
    _make_executable(pkg / ".venv" / "bin" / "python", "#!/bin/sh\n")

    result = subprocess.run(
        [str(entry), "--root", "/tmp", "foo"], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "real claudespace invoked with: --root /tmp foo"


def test_self_heals_by_invoking_the_provisioner_when_venv_python_is_missing(
    fake_package,
):
    pkg, entry = fake_package
    _make_executable(
        pkg / "scripts" / "provision.js",
        "#!/usr/bin/env node\nconsole.log('fake provisioner ran');\n",
    )

    result = subprocess.run([str(entry), "hello"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "fake provisioner ran" in result.stdout
    assert "real claudespace invoked with: hello" in result.stdout


def test_treats_a_non_executable_venv_python_as_needing_provisioning(fake_package):
    pkg, entry = fake_package
    (pkg / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")  # not chmod +x
    _make_executable(
        pkg / "scripts" / "provision.js",
        "#!/usr/bin/env node\nconsole.log('fake provisioner ran');\n",
    )

    result = subprocess.run([str(entry), "hello"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "fake provisioner ran" in result.stdout


def test_resolver_follows_a_relative_symlink_target(fake_package, tmp_path):
    pkg, _entry = fake_package
    _make_executable(pkg / ".venv" / "bin" / "python", "#!/bin/sh\n")

    global_bin = tmp_path / "globalbin"
    relative_link = global_bin / "claudespace-relative"
    os.remove(global_bin / "claudespace")
    os.symlink(os.path.relpath(pkg / "bin" / "claudespace.sh", global_bin), relative_link)

    result = subprocess.run([str(relative_link), "x"], capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.strip() == "real claudespace invoked with: x"
