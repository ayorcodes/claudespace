"""``claudespace update``: bring the installed claudespace up to date.

Routes by which channel installed the running binary (D6, ``channel.py``):
the npm channel re-installs the published package; the pipx channel mirrors
what ``install.sh`` does for a fresh install - clone the repo into a
throwaway temp directory (pipx installs a built wheel, not a live checkout,
so there's no local clone to ``git pull``), ``pipx install --force`` from
it. Neither branch re-runs asset sync itself any more - the first-run
sentinel (``assets_sync.sync_if_needed``, AD5) is per-version, so the next
``claudespace`` invocation after either upgrade re-syncs on its own.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile

from claudespace import channel as channel_module

logger = logging.getLogger(__name__)

REPO_URL = "https://github.com/ayorcodes/claudespace.git"
NPM_PACKAGE = "@ayorcodes/claudespace"


def _require(tool: str, hint: str) -> None:
    if shutil.which(tool) is None:
        logger.error("'%s' is required to update claudespace. %s", tool, hint)
        sys.exit(1)


def _base_python() -> str | None:
    """The real interpreter behind this process, or ``None`` if not found.

    ``sys.executable`` is the pipx venv's own ``bin/python``, which stops
    existing the moment the venv is removed. ``sys._base_executable`` is the
    interpreter the venv was *created from* (e.g. Homebrew's python3.14) and
    outlives it, which is what an update has to reinstall against.

    Returns ``None`` when nothing usable is found, so the caller can let pipx
    pick rather than passing a path that doesn't exist.
    """
    for candidate in (getattr(sys, "_base_executable", None), sys.executable):
        if candidate and os.path.isfile(candidate) and not _in_pipx_venv(candidate):
            return candidate
    return None


def _in_pipx_venv(path: str) -> bool:
    """Whether ``path`` lives inside a pipx venv, i.e. is about to be deleted."""
    return f"{os.sep}pipx{os.sep}venvs{os.sep}" in path


def run_update() -> None:
    """Bring claudespace up to date, routed by install channel (D6).

    Never falls through to the pipx path when the running install is npm -
    that would leave two competing installs on the machine instead of
    updating the one actually in use.
    """
    channel = channel_module.installed_channel()
    if channel == "npm":
        _run_npm_update()
    else:
        _run_pipx_update()
    logger.info("claudespace is up to date.")


def _run_npm_update() -> None:
    _require(
        "npm",
        "npm is required to update an npm-installed claudespace. Install "
        "Node.js (https://nodejs.org) and re-run.",
    )
    logger.info("Updating %s via npm...", NPM_PACKAGE)
    install = subprocess.run(["npm", "install", "-g", f"{NPM_PACKAGE}@latest"])
    if install.returncode != 0:
        logger.error(
            "'npm install -g %s@latest' failed - see output above.", NPM_PACKAGE
        )
        sys.exit(1)


def _run_pipx_update() -> None:
    """Re-clone the repo and reinstall via pipx."""
    _require("git", "Install git and re-run.")
    _require(
        "pipx",
        "Install pipx (https://pipx.pypa.io) and re-run, or reinstall via "
        "install.sh.",
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        logger.info("Cloning latest claudespace into a temporary directory...")
        clone = subprocess.run(
            ["git", "clone", "--depth", "1", REPO_URL, tmp_dir],
        )
        if clone.returncode != 0:
            logger.error("git clone failed - see output above.")
            sys.exit(1)

        logger.info("Installing claudespace from %s...", tmp_dir)
        # Uninstall first rather than `pipx install --force`: pipx ignores
        # `--python` when forcing into an existing venv, and reusing that venv
        # strands console scripts a newer version no longer declares.
        #
        # The interpreter has to be resolved *before* the uninstall, and it
        # must not be sys.executable: this process runs from inside the pipx
        # venv, so sys.executable points at
        # ~/.local/pipx/venvs/claudespace/bin/python - which the uninstall
        # then deletes. Passing it to the install afterwards failed with "No
        # executable for the provided Python version", leaving the machine
        # with claudespace uninstalled and the update half-done.
        python = _base_python()
        install_cmd = ["pipx", "install"]
        if python:
            install_cmd += ["--python", python]

        subprocess.run(
            ["pipx", "uninstall", "claudespace"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        install = subprocess.run([*install_cmd, tmp_dir])
        if install.returncode != 0 and python:
            # Don't leave the machine with nothing installed just because the
            # pinned interpreter is unusable - retry letting pipx choose.
            logger.warning(
                "Install with --python %s failed; retrying with pipx's "
                "default interpreter.",
                python,
            )
            install = subprocess.run(["pipx", "install", tmp_dir])
        if install.returncode != 0:
            logger.error(
                "pipx install failed - see output above. claudespace is "
                "currently uninstalled; recover with:\n"
                "    pipx install git+%s",
                REPO_URL,
            )
            sys.exit(1)


def main() -> None:
    """Entrypoint installed as the ``claudespace-update`` console script."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    run_update()


if __name__ == "__main__":
    main()
