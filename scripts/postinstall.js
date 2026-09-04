#!/usr/bin/env node
"use strict";

/**
 * npm postinstall (D1 -> D4 -> D7): assert the install is global, provision
 * the venv, then run an OS-aware preflight. Never syncs assets under
 * `~/.claude` - that happens on first real run instead, where the process
 * identity is right even under `sudo npm i -g` (D4).
 */

const { execFileSync, spawnSync } = require("child_process");
const path = require("path");

const { provision } = require("./provision.js");

const PKG_ROOT = path.resolve(__dirname, "..");
const PACKAGE_NAME = require("../package.json").name;

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
  const resolvedGlobalRoot = path.resolve(globalRoot);
  const resolvedPkgRoot = path.resolve(PKG_ROOT);
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
