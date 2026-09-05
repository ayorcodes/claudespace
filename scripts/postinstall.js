#!/usr/bin/env node
"use strict";

/**
 * npm postinstall (D1 -> D4 -> D7): assert the install is global, provision
 * the venv, then run an OS-aware preflight. Never syncs assets under
 * `~/.claude` - that happens on first real run instead, where the process
 * identity is right even under `sudo npm i -g` (D4).
 */

const { execFileSync, spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const { provision } = require("./provision.js");

const PKG_ROOT = path.resolve(__dirname, "..");
const PACKAGE_NAME = require("../package.json").name;

// Resolves symlinks where possible (e.g. macOS's /tmp -> /private/tmp, or a
// symlinked nvm/Homebrew prefix) so the containment check below compares
// real paths rather than tripping over a cosmetic symlink difference
// between otherwise-identical locations. Falls back to a plain
// normalization when the path can't be resolved (shouldn't happen for
// either input here, but this must never throw).
function realOrResolved(p) {
  try {
    return fs.realpathSync(p);
  } catch {
    return path.resolve(p);
  }
}

function npmGlobalRoot() {
  if (process.env.npm_config_prefix) {
    const suffix = process.platform === "win32" ? "" : path.join("lib", "node_modules");
    return path.join(process.env.npm_config_prefix, suffix);
  }
  try {
    return execFileSync("npm", ["prefix", "-g"]).toString().trim() +
      (process.platform === "win32" ? "" : path.sep + path.join("lib", "node_modules"));
  } catch {
    return null;
  }
}

function assertGlobalInstall() {
  const globalRoot = npmGlobalRoot();
  if (!globalRoot) {
    // Can't determine the global root at all - don't block the install on
    // an environment we can't inspect; a genuinely non-global install still
    // gets caught at runtime the first time a bare console-script name
    // fails to resolve on PATH.
    return;
  }
  // The global root directory (<prefix>/lib/node_modules) may not exist yet
  // on a from-scratch prefix - realpath that case falls back to a plain
  // resolve via realOrResolved, which is fine: it still compares correctly
  // against PKG_ROOT, which always exists (it contains this very script).
  const resolvedGlobalRoot = realOrResolved(globalRoot);
  const resolvedPkgRoot = realOrResolved(PKG_ROOT);
  if (
    resolvedPkgRoot === resolvedGlobalRoot ||
    resolvedPkgRoot.startsWith(resolvedGlobalRoot + path.sep)
  ) {
    return;
  }
  console.error(
    `error: ${PACKAGE_NAME} must be installed globally - the Claude Code ` +
      "hooks it registers are invoked by bare command name through PATH, " +
      "which only a global install puts there.\n" +
      `Run: npm install -g ${PACKAGE_NAME}`
  );
  process.exit(1);
}

function preflightDarwin() {
  const venvClaudespace = path.join(PKG_ROOT, ".venv", "bin", "claudespace");
  const result = spawnSync(venvClaudespace, ["doctor", "--yes", "--no-launch"], {
    stdio: "inherit",
    // postinstall may run as root under `sudo npm i -g` (D4) - this must
    // not trigger the first-run asset sync, which writes under the real
    // user's `~/.claude`/`~/.ai` and is only correct at a real, user-
    // identity invocation. See the matching check in cli.py's main().
    env: { ...process.env, CLAUDESPACE_SKIP_ASSET_SYNC: "1" },
  });
  if (result.status !== 0) {
    console.warn(
      "warning: claudespace doctor reported issues - see above. Run " +
        "'claudespace doctor' again after fixing them."
    );
  }
}

function preflight() {
  if (process.platform === "darwin") {
    preflightDarwin();
    return;
  }
  console.error(
    `error: claudespace preflight on ${process.platform} is not implemented yet.`
  );
  process.exit(1);
}

function main() {
  assertGlobalInstall();
  provision(PKG_ROOT);
  preflight();
}

main();
