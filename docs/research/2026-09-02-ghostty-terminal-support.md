# Original Request

"how can we support ghostty ?" — user wants claudespace to work with the Ghostty terminal emulator, not just iTerm2.

---

# Summary

claudespace's pane/session orchestration (window/tab/split creation, prompt injection, role handoff, workspace discovery) is implemented entirely against the `iterm2` Python API in `claudespace/iterm.py` and consumed directly by 11 of ~15 top-level modules. There is no abstraction layer between "terminal automation" and "iTerm2" today — every caller imports `iterm2` types (`iterm2.Session`, `iterm2.Window`, `iterm2.App`) and calls `claudespace.iterm.*` functions directly. Supporting Ghostty requires introducing a backend abstraction and a second implementation, not a small patch to one file.

---

# Current Behaviour

`claudespace/iterm.py` (775 lines) is the sole terminal-automation surface. It:
- builds/tears down the pane layout for a workspace (`build_workspace`, `_launch_pane`)
- finds and reveals role panes (`find_workspace_window`, `find_role_session`, `reveal_role`, `activate_session`, `activate_window`)
- injects and confirms role prompts via literal keystroke/paste simulation and screen-content polling (`send_role_prompt`, `_wait_for_claude_prompt`, `_screen_contains`, `_confirm_submitted`)
- reads/writes iTerm2 session user-variables for workspace state (`_get_workspace_var`, `get_auto_handoff`, `get_lazy`, `get_template_name`, `get_run_doc`, `set_run_doc`)
- iterates panes in a workspace window (`each_workspace_session`)

All of this depends on iTerm2's native Python API (`iterm2` package, installed at `.venv/lib/python3.13/site-packages/iterm2/`), which exposes an RPC connection to a running iTerm2 instance (`iterm2.Connection`, `iterm2.App`, `iterm2.Window`, `iterm2.Session`) — a scriptable object model with no Ghostty equivalent at the same fidelity.

---

# Affected Surfaces

Every one of these imports/uses `claudespace.iterm` or `iterm2` types directly (found via `grep -rln iterm claudespace/*.py`):

- `claudespace/cli.py` — command entry points that call into iterm.py
- `claudespace/connect.py` — connects to the running iTerm2 app instance
- `claudespace/config.py` — references iterm-specific config
- `claudespace/environment.py` — environment/session setup
- `claudespace/handoff.py` (635 lines) — role-to-role handoff, session discovery/reveal
- `claudespace/layouts.py` — pane layout definitions consumed by `build_workspace`
- `claudespace/messaging.py` — `claudespace-msg` ad hoc pane messaging
- `claudespace/themes.py` — pane/session theming
- `claudespace/utils.py` — shared helpers referencing iterm types
- `claudespace/watchdog.py` (189 lines) — session polling/monitoring
- `claudespace/workspace.py` (95 lines) — workspace lifecycle, window discovery

Each would need to route through a new backend interface instead of `iterm2` directly. None can be skipped — the object model (`Session`, `Window`, `App`) is threaded through all of them as the unit of identity for a pane.

---

# Existing Implementation & Placement

**Existing implementation**: No Ghostty support exists anywhere in the repo — confirmed via `grep -rl "ghostty" claudespace/` (no matches) and file listing (only `iterm.py`, no `backend.py`/`terminal.py` abstraction file). This is a greenfield addition, not an extension of partial work.

**Correct home**: Single-package repo (`claudespace/`), no monorepo/shared-package structure — the change belongs in this package. No `CLAUDE.md` guidance on terminal-backend placement exists (checked `/Users/ayorcodes/.claude/CLAUDE.md` — only communication/attribution rules, nothing project-structural). Within the package, the natural split (not yet built) is a `PaneBackend`-style interface with `ItermBackend` (wrapping current `iterm.py` logic) and `GhosttyBackend` implementations, selected via `config.py`.

---

# Execution Flow

```
CLI command (cli.py)
    ↓
workspace.py / handoff.py  (role dispatch, pane reveal/handoff logic)
    ↓
iterm.py  (build_workspace, reveal_role, send_role_prompt, etc.)
    ↓
iterm2 package  (RPC connection to running iTerm2.app)
```

watchdog.py polls session state independently via the same `iterm2` connection.

---

# Relevant Files

- `claudespace/iterm.py` — the entire terminal-automation implementation; every function is iTerm2-API-specific.
- `claudespace/handoff.py` — largest consumer; role handoff and pane-reveal logic built on `iterm.py`'s session/window objects.
- `claudespace/workspace.py` — workspace-level lifecycle (build/window discovery), thin wrapper over `iterm.py`.
- `claudespace/watchdog.py` — background session polling against the same API.
- Remaining files (`cli.py`, `connect.py`, `config.py`, `environment.py`, `layouts.py`, `messaging.py`, `themes.py`, `utils.py`) — confirmed via grep to reference `iterm`/`iterm2`, not individually traced (out of scope until a design decides the abstraction boundary).

---

# Relevant Components

- Pane/window orchestration (`iterm.py`, `workspace.py`)
- Role handoff and pane reveal (`handoff.py`)
- Session monitoring (`watchdog.py`)
- Ad hoc pane messaging (`messaging.py`)

---

# Existing Constraints

- `iterm.py` relies on iTerm2 session **user-variables** for workspace state persistence (`_get_workspace_var`, `get_run_doc`/`set_run_doc`) — this is an iTerm2-specific persistence mechanism with no documented Ghostty equivalent (unverified — see Unknowns).
- Role-prompt injection/confirmation (`send_role_prompt`, `_confirm_submitted`) works by simulating input and polling rendered screen content — mechanism-specific to iTerm2's Python API screen-streaming; a Ghostty backend would need an equivalent (Ghostty 1.3's AppleScript API preview, or the third-party `ghostty-automator` IPC fork, per prior web research this session — not verified against actual API docs in this investigation).

---

# Existing Behaviour

- `each_workspace_session` and `find_workspace_window` locate panes by a marker/instance convention stored in session variables — any new backend must preserve this identity model or the rest of the pipeline (conductor → researcher → planner → ... handoff chain) breaks.

---

# Unknowns

- `Q: Does Ghostty's automation surface (native AppleScript API or ghostty-automator fork) support session user-variables or an equivalent for persisting workspace/role/marker state? -> [engineering - unresolved]` — not verified in this investigation (no Ghostty API docs were inspected, only prior general web search). Must be checked before a design can commit to preserving the current state-persistence model.
- `Q: Should claudespace support Ghostty now, given Ghostty's automation API is an explicit 1.3 "preview" likely to break in 1.4? -> [product]` — a prioritization/risk-tolerance call, not answerable from the repo.

---
