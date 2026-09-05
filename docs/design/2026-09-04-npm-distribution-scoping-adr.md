# ADR: npm as the primary distribution channel (scoping)

Status: **Accepted** 2026-09-04 — decision settled, **no implementation
started**. Depends on, but does not gate,
`docs/design/2026-09-03-windows-support-psmux-scoping-adr.md` (still
Proposed): the npm work can land on macOS alone, with Windows slotting in
behind the psmux spike.

Date: 2026-09-04

## Context

claudespace ships today as a Python package installed with pipx, driven by
`install.sh` (curl | sh) and updated by `claudespace update`, which re-clones
the repo into a temp dir and `pipx install`s it (`update.py`). The whole path
assumes macOS: `install.sh` refuses non-Darwin in its first 20 lines, and
falls back to Homebrew for both Python and pipx.

Two forces make that the wrong shape going forward:

1. **Discovery.** The audience is Claude Code users, who overwhelmingly have
   node and reach for `npm i -g` before they reach for pipx. `curl | sh` is a
   trust ask that npm does not make.
2. **Windows.** The psmux ADR opens a credible native-Windows path. pipx on
   Windows is a materially worse experience than pipx on macOS — no Homebrew
   fallback, a `py` launcher instead of `python3`, and PATH/Scripts-dir
   friction. node's installer story on Windows is first-class. If Windows is
   real, the installer must stop being a POSIX shell script.

This ADR decides **how claudespace is distributed and provisioned**, and how
the installer detects the environment it landed in. It does *not* decide
anything about terminal backends.

### What is not on the table

Rewriting claudespace in Node. The core depends on iTerm2's Python API, and
the tmux/psmux backends are subprocess drivers with 48 headless integration
tests behind them. npm is a **delivery wrapper around the existing Python
package** — the Python tree ships inside the npm tarball (~400 KB including
`claudespace/assets/`), so nothing is cloned or fetched at install time
beyond the Python dependency itself.

## Decision

**npm becomes the primary, documented channel:
`npm i -g @ayorcodes/claudespace`.**
pipx/PyPI remains supported for Python-native users but stops being the
path the README leads with. `install.sh` reduces to a shim that installs
node if absent and then calls npm, or is retired outright.

The following are load-bearing, not implementation detail. Each exists
because the naive version of it is broken.

### D0 — The package is scoped: `@ayorcodes/claudespace`

Settled empirically, not by preference (see the naming entry under
*Consequences*): unscoped `claudespace` is permanently unavailable, blocked by
the registry's similarity check against the existing `claude-space`. The scope
is the route npm's own error message recommends, and scoped names are exempt
from that check. `claudespace-cli` and `claudespace-code` were considered as
unscoped fallbacks and rejected — both carry the same untested similarity risk
for no benefit over the scope.

`bin` stays `{ claudespace: … }`, so the install line is the only thing that
changes; the command users type daily is unaffected. A scope cannot be claimed
by anyone else, so there is **no placeholder publish to make** — the first
publish is the real one.

### D1 — Global install only; local and `npx` are refused

The Claude Code Stop hook is registered as the bare command
`claudespace-handoff` (`assets_sync.py:50`) and resolved through PATH every
time the hook fires. Only a global install puts the console scripts on PATH.
An `npx` install resolves into a versioned cache that moves on every release,
and a project-local install disappears on the next `npm ci`. Either one leaves
a registered hook pointing at nothing — a silently dead pipeline, which is the
worst possible failure for this tool.

**postinstall must detect a non-global install and fail loudly**, naming the
correct command, rather than half-installing.

### D2 — The venv lives inside the package directory

Provision with a plain `python -m venv` (or `uv venv`) into
`<package>/.venv`, **not** `uv tool install` or a nested pipx. Those install
outside `node_modules`, so `npm rm -g claudespace` would remove the shims and
orphan the environment. Keeping the venv inside the package directory makes
npm's own uninstall complete, and makes a version bump's rebuild automatic.

### D3 — Shims are `sh`/`cmd`, not JavaScript

npm `bin` entries may point at any executable with a shebang. The handoff hook
fires on every Stop event across every role pane; putting a ~40 ms node
startup in that path is a real tax for no benefit. Use `#!/bin/sh` on POSIX
and let npm generate the `.cmd`/`.ps1` wrappers on Windows.

### D4 — postinstall never writes to `~/.claude` as root

`sudo npm i -g` runs postinstall as root. `sync_assets()` writes to
`Path.home() / ".claude"` and `~/.ai/prompts` (`assets_sync.py:45-47`); as
root that either targets root's home or leaves root-owned files in the user's,
which then fail to update on every subsequent run. **Asset sync moves out of
postinstall and into first-run**, where the process identity is right. The
postinstall does venv provisioning only.

### D5 — Shims self-heal when postinstall was skipped

`--ignore-scripts` is a common CI setting and pnpm's default. The shims must
detect a missing or stale `.venv` and provision it on first invocation rather
than erroring. This costs a slow first run, which is strictly better than an
install that silently produced nothing.

### D6 — One channel per machine, enforced by `doctor`

A user with both a pipx and an npm install gets two `claudespace` binaries
whose precedence depends on PATH order, and updating one leaves the other
stale and shadowing. `doctor` must detect the competing install and say which
is winning. `claudespace update` (`update.py`) must detect the channel it was
installed through — a marker file written at provision time — and route to
`npm i -g claudespace@latest` or the existing pipx path accordingly, rather
than unconditionally pipx-installing over an npm install.

### D7 — Environment detection is per-OS and happens at install

`install.sh` currently front-loads the terminal/CLI preflight so a new user is
not bounced out of the tool two or three times. Keep that property, but make
it OS-aware by delegating to `environment.detect_usable_backends()` rather
than re-implementing it in the installer:

- **darwin** — iTerm2 (+ its Python API), or tmux + a viewer, or cmux.
- **win32** — psmux + a host terminal, per the psmux ADR. No viewer app is
  required: psmux draws its own panes inside the terminal it was invoked from,
  so Ghostty/iTerm2/cmux have no Windows analogue and need none.
- **anything else** — refuse, as today.

Python discovery has to be reimplemented per-OS: `install.sh`'s `find_python`
walks `python3.14 … python3` plus Homebrew paths, none of which exist on
Windows, where the `py` launcher is the right probe.

### D8 — Python 3.12+ remains a hard prerequisite

npm does not remove it; it only moves who is responsible for finding it. The
postinstall keeps `install.sh`'s ordering — probe for a usable interpreter,
then offer to install one (Homebrew on macOS, winget on Windows) — and fails
with the same actionable message when it cannot.

## Scope

**In scope**
- The npm package: layout, `bin` shims, postinstall provisioning, `"os"` field.
- Moving asset sync from install time to first run (D4).
- Install-channel detection and routing in `update.py` and `doctor` (D6).
- OS-aware preflight and Python discovery (D7, D8).
- Reducing or retiring `install.sh`.

**Out of scope (explicitly)**
- Rewriting any part of claudespace in Node.
- Platform-specific prebuilt binaries (PyInstaller and friends). That trades
  the Python prerequisite for a codesigning, notarization and arch-matrix
  problem, and is a separate decision if the Python prereq proves fatal.
- All Windows *backend* work — psmux selection, viewer, `osascript` and
  darwin-gate replacement. See the psmux ADR.
- Windows reboot persistence, already out of scope there.

## Consequences

**If it works out**
- One install command on both platforms, no `curl | sh`, and an uninstall that
  actually removes everything.
- The Windows story becomes deliverable: node's Windows installer carries the
  weight that Homebrew carries on macOS today.

**Risks accepted / to watch**
- **Two package managers in one install path.** npm provisions a Python venv;
  failures now come from either. The error messages have to name which layer
  failed, or this is worse than pipx.
- **A new node prerequisite** for existing macOS users who had none. Mitigated
  by keeping pipx working (D6).
- **Slower, network-dependent install** — postinstall creates a venv and pulls
  `iterm2` from PyPI. Perceptibly slower than `npm i -g` on a pure-JS package,
  and it can fail offline.
- **Registry naming — RESOLVED 2026-09-04. Unscoped `claudespace` is
  unavailable.** A publish attempt was rejected by the registry:
  `E403 ... Package name too similar to existing package claude-space; try
  renaming your package to '@ayorcodes/claudespace'`. The blocking incumbent,
  `claude-space` (v2.0.3, 2026-01-22), is a real 17 MB Electron/Vue-Flow
  "visual agent workflow builder for Claude" built on
  `@anthropic-ai/claude-agent-sdk` — not a squat, so there is no dispute
  route. npm's similarity check normalizes punctuation, so no unscoped
  spelling that differs from `claude-space` only by the hyphen can ever be
  registered. **The name is therefore scoped — see D0.** In every case `bin` stays
  `{ claudespace: … }`, so the command users type daily is unaffected; only
  the install line changes. A secondary consequence stands regardless: the
  incumbent is domain-adjacent enough to cause user confusion on its own.

- **Two supported channels is a support burden**, which D6 mitigates but does
  not remove.

## References

- Windows/psmux scoping: `docs/design/2026-09-03-windows-support-psmux-scoping-adr.md`
- psmux go/no-go spike: `docs/research/2026-09-03-psmux-windows-spike.md`
- Current installer: `install.sh` · current updater: `claudespace/update.py`
- Hook registration: `claudespace/assets_sync.py`
