"""``claudespace update``: pull the latest code from git and reinstall.

Mirrors what ``install.sh`` does for a fresh install - clone the repo into a
throwaway temp directory (pipx installs a built wheel, not a live checkout,
so there's no local clone to ``git pull``), ``pipx install --force`` from
it, then re-run ``sync_assets`` so any updated bundled commands/prompts
overwrite what's in ``~/.claude/commands`` and ``~/.ai/prompts``.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile

from claudespace.assets_sync import sync_assets

logger = logging.getLogger(__name__)

REPO_URL = "https://github.com/ayorcodes/claudespace.git"


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
    """Re-clone the repo, reinstall via pipx, and resync bundled assets."""
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

    logger.info("Registering bundled commands and prompts...")
    sync_assets()

    logger.info("claudespace is up to date.")


def main() -> None:
    """Entrypoint installed as the ``claudespace-update`` console script."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    run_update()


if __name__ == "__main__":
    main()
