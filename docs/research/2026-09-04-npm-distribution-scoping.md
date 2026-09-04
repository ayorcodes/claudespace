# Original Request

`docs/design/2026-09-04-npm-distribution-scoping-adr.md` — ADR: npm as the
primary distribution channel (scoping). Accepted, no implementation started.
Decisions D0-D8 define the npm package layout, install-channel detection,
OS-aware preflight, and moving asset sync from postinstall to first-run.

---

# Summary

Investigated the current pipx/`install.sh`-based distribution path the ADR
proposes to replace/wrap, to ground the eight load-bearing decisions (D0-D8)
in the actual code they touch. Confirmed: 7 console-script entry points (not
just the one the ADR names), the exact postinstall-time filesystem writes
D4 flags, the macOS-hardcoded preflight D7 must generalize, and that no npm
scaffolding exists yet anywhere in the repo — this is greenfield.

---

# Current Behaviour

- `install.sh` (177 lines): refuses non-Darwin immediately (`uname -s !=
  Darwin`), clones the repo (or uses `$0`'s dir if run from a checkout),
  locates a Python ≥3.12 (`python3.14`→`python3.12`, then Homebrew paths),
  installs Homebrew Python if none found, installs pipx (via Homebrew or
  `pip install --user`), fixes shell-rc trailing newlines before `pipx
  ensurepath`, then `pipx uninstall` + `pipx install --python "$PYTHON"
  "$SCRIPT_DIR"` (uninstall-then-install, not `--force`, because `--force`
  silently ignores `--python` on an existing venv and leaves orphaned
  console scripts — `install.sh:135-142`). Runs `claudespace-sync-assets`,
  then `claudespace doctor --yes --no-launch`, then verifies PATH in a
  fresh `zsh -lic` login shell.
- `claudespace/update.py` (`run_update`): mirrors install.sh — clones to a
  temp dir, resolves the *base* interpreter via `sys._base_executable`
  (not `sys.executable`, which lives inside the pipx venv about to be
  deleted — `update.py:32-46`), uninstalls, reinstalls, falls back to
  pipx's default interpreter if the pinned one fails, then calls
  `sync_assets()` directly (in-process, not via the console script).
- `pyproject.toml` `[project.scripts]` registers **7** console scripts, not
  just `claudespace-handoff`: `claudespace`, `claudespace-sync-assets`,
  `claudespace-handoff`, `claudespace-guard`, `claudespace-msg`,
  `claudespace-update`, `claudespace-tmux-persist`. All must remain on PATH
  post-install for D1 to hold — the guard hook (PreToolUse) and msg/tmux-
  persist scripts are just as PATH-dependent as the Stop hook the ADR
  names.
- `claudespace/assets_sync.py` (`sync_assets`, module docstring +
  `DEFAULT_CONFIG_DIR`/`PROMPTS_DEST` at lines 45-47): writes bundled
  slash-commands to every discoverable `<CLAUDE_CONFIG_DIR>/commands`
  (there can be several — `~/.claude`, `~/.claudeMax`, etc., see
  `claude_config_dirs()`), prompts to `~/.ai/prompts`, and registers the
  Stop/PreToolUse hooks in each config's `settings.json`. Always
  overwrites with the bundled version (no preservation of local edits).
  Currently invoked by `install.sh` (as the console script, post pipx-
  install) and by `update.py` (in-process). This confirms D4's premise:
  everything it touches is under `Path.home()`, so running it as root
  (via `sudo npm i -g`) would target root's home or leave root-owned
  files behind.
- `claudespace/environment.py`: `require_macos()` (`sys.platform !=
  "darwin"` → exit) is called at the top of `run_doctor_checks` and is
  the actual current hard macOS gate at the Python layer (independent of
  `install.sh`'s own Darwin check). `detect_usable_backends()` is already
  the single source of truth `run_doctor_checks`/`install.sh` defer to for
  "is any supported terminal usable" (iTerm2 via `is_iterm_installed()`,
  tmux via `tmux_cli.is_tmux_available()` + configured viewer, cmux via
  `is_cmux_installed()`/`is_cmux_reachable()`) — this is exactly the
  function D7 says the installer should keep delegating to rather than
  reimplementing, it just doesn't yet branch per-OS internally.
  `is_brew_available()`/`_brew()` and `install_iterm_via_brew()` are the
  Homebrew-specific fallback D8 keeps for macOS and D7 says needs a
  Windows analogue (winget).

---

# Affected Surfaces

This ADR is scoping/decision-only (Status: Accepted, no implementation
started) — no code changes were made to investigate as "already shipped."
Per step 3, the surfaces a real implementation will need to touch (not
consumers of an existing contract, since none of D0-D8 is implemented yet):

- `install.sh` — reduced to a node-bootstrap shim or retired (D0, in scope).
- `claudespace/update.py` — needs channel detection + npm-vs-pipx routing
  (D6); currently pipx-only, would silently pipx-reinstall over an npm
  install today if left unchanged.
- `claudespace/assets_sync.py` — `sync_assets()` call site needs to move
  from install/update-time to first-run (D4); the function itself doesn't
  need to change, only when it's invoked.
- `claudespace/environment.py` — `require_macos()` and the doctor/install
  entry points need an OS branch (D7); `detect_usable_backends()` itself
  is reusable as-is per the ADR's own reasoning.
- `pyproject.toml` `[project.scripts]` — the full 7-entry list is the
  contract the npm `bin` shims (D3) and the "PATH must have everything"
  requirement (D1) have to preserve; not just the one hook the ADR body
  text names.
- No frontend/other-repo consumers found — this is a single-repo CLI
  distribution change with no cross-service callers.

---

# Existing Implementation & Placement

**Existing implementation**: None. No `package.json`, `.npmignore`, or any
npm-related file exists anywhere in the repo (verified by search). No prior
npm distribution attempt, placeholder publish, or partial scaffolding to
build on — the ADR's own text ("no placeholder publish to make... the first
publish is the real one") is consistent with what's on disk.

**Correct home**: Single-repo project (`workspace-launcher`, package name
`claudespace`) with no monorepo/shared-package structure — `pyproject.toml`
is the only package manifest, and there is no upstream/library boundary to
consider. The new npm package layout, shims, and `postinstall` belong in
this same repo, most likely at the repo root (sibling to `pyproject.toml`)
since D2 requires the venv to live inside the npm package directory and D3's
shims need to sit next to the Python tree they wrap. `CLAUDE.md`'s
"Engineering rules" section documents macOS-only and `TerminalBackend`
conventions but says nothing about a distribution/packaging directory
convention, so there is no existing doc instruction to defer to here — this
is a genuinely new area of the tree.

No prior memory note exists for this feature area (checked
`docs/design/` and `docs/research/` for a `*-notes.md` sibling to the ADR;
none found — this is the first design artifact on npm distribution).

---

# Execution Flow

Current (pipx):
```
curl install.sh
    ↓ (Darwin check, find_python, find_brew)
pipx install <repo>
    ↓
claudespace-sync-assets  (writes ~/.claude/commands, ~/.ai/prompts, settings.json hooks)
    ↓
claudespace doctor --yes --no-launch  (require_macos, detect_usable_backends, iTerm2 API enable)
```

Update path:
```
claudespace-update
    ↓
update.py: git clone tmp → resolve _base_python → pipx uninstall/install → sync_assets() in-process
```

The ADR's proposed flow (not yet implemented) reorders this: `npm i -g` →
postinstall (venv provisioning only, D4) → first shim invocation (asset
sync + self-heal venv, D5) → `doctor`/`update` gaining channel-detection
branches (D6).

---

# Relevant Files

- `install.sh` — current installer; D0/D7's baseline to replace or shim.
- `claudespace/update.py` — current updater; D6's channel-routing target.
- `claudespace/assets_sync.py` — postinstall filesystem writes; D4's target.
- `claudespace/environment.py` — macOS gate + backend detection; D7/D8's target.
- `pyproject.toml` — console-script contract (`[project.scripts]`) that
  D1/D3's npm `bin` shims must fully preserve.

---

# Relevant Components

- Installer (`install.sh`)
- Updater (`claudespace/update.py`)
- Asset sync / hook registration (`claudespace/assets_sync.py`)
- Environment preflight / doctor (`claudespace/environment.py`)
- Package manifest / console scripts (`pyproject.toml`)

---

# Existing Constraints

- 7 console scripts must all resolve on PATH post-install, not just
  `claudespace-handoff` (`pyproject.toml` `[project.scripts]`).
- `sync_assets()` writes under `Path.home()` only (`~/.claude/*`,
  `~/.ai/prompts`) — no other paths, confirming D4's root-safety concern
  is scoped correctly.
- `require_macos()` is a hard `sys.exit(1)` gate inside
  `run_doctor_checks`, independent of `install.sh`'s own shell-level
  Darwin check — both currently exist and both would need updating for
  Windows (out of scope here per the psmux ADR, but D7's installer-level
  detection sits in front of this Python-level gate).
- `detect_usable_backends()` already centralizes "what terminal setup is
  usable" and is reused, not reimplemented, in both `install.sh`
  (indirectly via `claudespace doctor`) and `run_doctor_checks` — matches
  D7's explicit instruction to delegate rather than reimplement.
- pipx uninstall-then-install (not `--force`) is required to avoid stale
  console scripts — an npm/venv equivalent concern for D2's "rebuild is
  automatic on version bump."

---

# Existing Behaviour

- Both `install.sh` and `update.py` independently solve the same "don't
  pass a soon-to-be-deleted interpreter path to pipx" problem
  (`sys._base_executable` in Python, `find_python` shell probing in
  install.sh) — any D8 Python-discovery rewrite should keep these
  reconciled or unify them, since they currently duplicate the same fix.
- `sync_assets()` always overwrites bundled files with no diffing against
  local edits — this behavior is unchanged by D4, only the *timing* of
  the call moves.

---

# Unknowns

- Exact target location for the npm package files (`package.json`,
  shims) within the repo root vs. a subdirectory — not specified by the
  ADR or any existing convention. `[engineering - unresolved]` — this is
  an implementation-design decision (principal's call), not something
  the repository can answer.
- Whether `claudespace-update`'s existing pipx-specific logic
  (`update.py`) gets a new branch inline or a channel-detection module is
  factored out — ADR states the requirement (D6) but not the shape.
  `[engineering - unresolved]`, principal-level.

Both unknowns are implementation-design choices, not product/UX questions
the ADR left open — the ADR is unambiguous about desired end-state
behavior for every D0-D8 item.

---

# Routing

This is a well-scoped engineering change (installer/packaging rewrite) with
zero product/UX ambiguity — the ADR already resolved every open question
(including the registry-naming decision, empirically, per the Consequences
section) and both remaining unknowns above are engineering-design choices,
not product questions. **Routing directly to principal, skipping planner.**
