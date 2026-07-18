"""``claudespace update``: pull the latest code from git and reinstall.

Mirrors what ``install.sh`` does for a fresh install - clone the repo into a
throwaway temp directory (pipx installs a built wheel, not a live checkout,
so there's no local clone to ``git pull``), ``pipx install --force`` from
it, then re-run ``sync_assets`` so any updated bundled commands/prompts
overwrite what's in ``~/.claude/commands`` and ``~/.ai/prompts``.
"""

from __future__ import annotations

import logging
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
        install = subprocess.run(["pipx", "install", "--force", tmp_dir])
        if install.returncode != 0:
            logger.error("pipx install failed - see output above.")
            sys.exit(1)

    logger.info("Registering bundled commands and prompts...")
    sync_assets()

    logger.info("claudespace is up to date.")


def main() -> None:
    """Entrypoint installed as the ``claudespace:update`` console script."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    run_update()


if __name__ == "__main__":
    main()
