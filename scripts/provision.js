#!/usr/bin/env node
"use strict";

/**
 * Build (or rebuild) the `.venv` claudespace runs from, and write the
 * channel marker `claudespace/channel.py` reads (AD4/D6). Shared by
 * `postinstall.js` (fresh install / npm update) and by the committed shims'
 * self-heal path (D5, AD3) when postinstall never ran (`--ignore-scripts`,
 * pnpm). Only `darwin` is implemented (AD6) - `win32` is a seam for the
 * psmux work.
 */

const { execFileSync, spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const MIN_PYTHON_MINOR = 12;
const CHANNEL_MARKER_NAME = ".claudespace-channel";

const VERSION_CHECK_SNIPPET =
  `import sys; sys.exit(0 if sys.version_info[:2] >= (3, ${MIN_PYTHON_MINOR}) else 1)`;

function isUsablePython(candidate) {
  try {
    const result = spawnSync(candidate, ["-c", VERSION_CHECK_SNIPPET], {
      stdio: "ignore",
    });
    return result.status === 0;
  } catch {
    return false;
  }
}

function which(name) {
  try {
    if (process.platform === "win32") {
      return execFileSync("where", [name]).toString().trim().split(/\r?\n/)[0];
    }
    // "command" is a shell builtin, not an executable on PATH - it has to
    // be run through a shell rather than execFileSync'd directly. `name`
    // is always one of our own hardcoded candidate strings, never
    // user-controlled input, so interpolating it here is safe.
    return execFileSync("/bin/sh", ["-c", `command -v ${name}`]).toString().trim();
  } catch {
    return null;
  }
}

function findBrew() {
  const onPath = which("brew");
  if (onPath) return onPath;
  for (const candidate of ["/opt/homebrew/bin/brew", "/usr/local/bin/brew"]) {
    if (fs.existsSync(candidate)) return candidate;
  }
  return null;
}

// Mirrors install.sh's find_python probe order exactly, so the two
// installers behave identically on the same machine.
function findPythonDarwin() {
  const candidates = [
    "python3.14",
    "python3.13",
    "python3.12",
    "/opt/homebrew/bin/python3",
    "/usr/local/bin/python3",
    "python3",
  ];
  for (const candidate of candidates) {
    const resolved = candidate.startsWith("/") ? candidate : which(candidate);
    if (resolved && isUsablePython(resolved)) return resolved;
  }
  return null;
}

function findPythonWin32() {
  notImplemented("win32 Python discovery (py launcher + winget)");
}

function findPython() {
  if (process.platform === "darwin") return findPythonDarwin();
  if (process.platform === "win32") return findPythonWin32();
  notImplemented(`Python discovery on ${process.platform}`);
}

function notImplemented(what) {
  console.error(`error: ${what} is not implemented yet on this platform.`);
  process.exit(1);
}

function dieNoPython() {
  const brewHint =
    process.platform === "darwin"
      ? " ('brew install python@3.13')"
      : "";
  console.error(
    `error: claudespace needs Python 3.${MIN_PYTHON_MINOR} or newer.\n` +
      `Install it${brewHint} and re-run.`
  );
  process.exit(1);
}

function resolvePythonDarwin() {
  let python = findPythonDarwin();
  if (python) return python;

  const brew = findBrew();
  if (brew) {
    console.log("No Python 3.12+ found. Installing one via Homebrew...");
    const result = spawnSync(brew, ["install", "python@3.13"], {
      stdio: "inherit",
    });
    if (result.status === 0) {
      python = findPythonDarwin();
    }
  }
  if (!python) dieNoPython();
  return python;
}

function resolvePython() {
  if (process.platform === "darwin") return resolvePythonDarwin();
  if (process.platform === "win32") return findPythonWin32();
  notImplemented(`Python discovery on ${process.platform}`);
}

function venvPython(pkgRoot) {
  const bin = process.platform === "win32" ? "Scripts" : "bin";
  const exe = process.platform === "win32" ? "python.exe" : "python";
  return path.join(pkgRoot, ".venv", bin, exe);
}

function createVenv(python, venvDir) {
  console.log(`Creating virtualenv at ${venvDir}...`);
  const result = spawnSync(python, ["-m", "venv", venvDir], {
    stdio: "inherit",
  });
  if (result.status !== 0) {
    console.error("error: venv provisioning failed (python -m venv).");
    process.exit(1);
  }
}

function pipInstall(pkgRoot) {
  console.log(`Installing claudespace into the venv from ${pkgRoot}...`);
  const result = spawnSync(venvPython(pkgRoot), ["-m", "pip", "install", pkgRoot], {
    stdio: "inherit",
  });
  if (result.status !== 0) {
    console.error(
      "error: venv provisioning failed (pip). See the pip output above - " +
        "this is often an offline install or a transient PyPI failure. " +
        "Re-running claudespace will retry provisioning."
    );
    process.exit(1);
  }
}

function writeChannelMarker(pkgRoot) {
  const venvDir = path.join(pkgRoot, ".venv");
  fs.writeFileSync(path.join(venvDir, CHANNEL_MARKER_NAME), "npm");
}

function provision(pkgRoot) {
  if (process.platform !== "darwin") {
    notImplemented(`claudespace provisioning on ${process.platform}`);
  }
  const python = resolvePython();
  console.log(`Using ${python}`);
  const venvDir = path.join(pkgRoot, ".venv");
  createVenv(python, venvDir);
  pipInstall(pkgRoot);
  writeChannelMarker(pkgRoot);
  console.log("claudespace venv provisioned.");
}

module.exports = { provision, findPython, isUsablePython, findBrew, venvPython };

if (require.main === module) {
  const pkgRoot = path.resolve(__dirname, "..");
  provision(pkgRoot);
}
