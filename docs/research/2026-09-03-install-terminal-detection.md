# Original Request

> we need to update the install script to now force iTerm download especially
> if tmux, ghostty is available, we need a way to check for support for what
> we support then we can now suggest to download if nothing available,
> example, someone deleted ghostty, and doesn't have Iterm2, but they have
> cmux, how can we handle? support cmux?

# Summary

`install.sh` unconditionally runs `claudespace doctor`, which unconditionally
checks for and, if missing, auto-installs iTerm2 via Homebrew
(`environment.check_environment` → `is_iterm_installed()` /
`install_iterm_via_brew()`) — regardless of which terminal backend the user
actually has or will configure. A user set up for the `tmux` backend (which
needs no GUI terminal app of its own; it optionally spawns a viewer, default
`ghostty`) still gets forced through iTerm2 detection/install. There is no
"detect what's supported, suggest install only if nothing is" logic — only a
single hardcoded iTerm2 path. cmux is not a backend at all yet: it is
`Proposed`/gated in `docs/design/2026-09-03-cmux-backend-scoping-adr.md`, and
its go/no-go spike (`docs/research/2026-09-03-cmux-backend-spike.md`) has
**not been run** — no cmux code exists to detect or select today.

# Current Behaviour

`install.sh:154-155` calls `claudespace doctor --yes --no-launch`
unconditionally, with no backend selection at all.

`environment.check_environment()` (`claudespace/environment.py:260-287`) then
always:
1. Checks `is_claude_installed()` (the `claude` CLI) — backend-agnostic, fine.
2. Checks `is_iterm_installed()`; if false, calls
   `install_iterm_via_brew(assume_yes=...)`, which installs iTerm2 via
   `brew install --cask iterm2` (or fails hard if Homebrew is absent).
3. Calls `_ensure_api_enabled()` to turn on/verify iTerm2's Python API.

None of this consults `config.load_terminal_backend()` — the function that
already exists to read `CLAUDESPACE_TERMINAL` env or `~/.config/claudespace/
config.toml`'s `[terminal] backend`. `_resolve_backend()` in `cli.py:335-349`
does consult it, but only inside the main run path
(`_ensure_terminal_launched()`, `cli.py:377-401`), which already special-cases
`TmuxBackend` to skip iTerm2 entirely (`isinstance(backend, TmuxBackend):
return False`). Doctor and the install script never reach that logic — they
run before/outside a backend resolution.

`KNOWN_TERMINAL_BACKENDS = frozenset({"iterm2", "tmux"})`
(`claudespace/config.py:38`) is the complete list of what
`load_terminal_backend()`/`get_backend()` accept. There is no `"cmux"`, and
no "ghostty" backend (ghostty is only `TmuxBackend`'s optional *viewer*,
selected via `[terminal.tmux] viewer`, default `"ghostty"` —
`config.py:40,92-107` — not something doctor or install.sh detects as
terminal-support evidence).

`tmux_cli.is_tmux_available()` (`claudespace/backends/tmux_cli.py:47`) exists
and is already used for one *informational* doctor check
(`cli.py:_check_tmux_persistence`, `cli.py:352-374`) — it checks for the
vendored resurrect/continuum plugins, not for whether tmux itself is present
as a viable backend choice, and never affects doctor's exit code.

# Affected Surfaces

Purely explanatory/investigative up to this point, but the request implies a
change, so listing every surface that currently assumes "iTerm2 is the thing
to check/install":

- `install.sh` (`install.sh:150-166`) — hardcoded iTerm2-only framing in
  comments and the top-of-file macOS-only rationale ("claudespace only works
  on macOS (it drives iTerm2...)" — line 7); doesn't select or pass a backend
  to doctor.
- `environment.check_environment()` (`claudespace/environment.py:260-287`) —
  the actual unconditional iTerm2-install logic. Needs to become
  backend-aware (or backend-agnostic with a "some usable backend exists"
  check) to change.
- `cli.py` doctor subcommand invocation (`cli.py:428+`, need to re-read exact
  call) — currently calls `environment.check_environment` directly with no
  backend argument.
- `claudespace/config.py` `KNOWN_TERMINAL_BACKENDS` / `load_terminal_backend`
  — the registry a `"cmux"` value would need to join, if cmux support is
  built.
- `claudespace/backends/__init__.py` `get_backend()` — the dispatch a
  `CmuxBackend` would need a branch in, if built.

No frontend/other-codebase consumers — this is a single-package CLI tool with
no external API contract into this behaviour.

# Existing Implementation & Placement

**Existing implementation:** No multi-backend detection/suggestion logic
exists anywhere. `is_tmux_available()` exists but is used only for the
resurrect-plugin informational check, not as a "you already have a working
backend, skip iTerm2" signal. No `is_ghostty_installed()` or
`is_cmux_installed()` equivalent exists. No `CmuxBackend` exists — cmux is
only a proposed ADR + an unrun spike; `KNOWN_TERMINAL_BACKENDS` has no
`"cmux"` entry and `get_backend()` would raise `ValueError` on it today.

**Correct home:** `claudespace/environment.py` is the existing, correct home
for "detect what's installed / offer to install what's missing" — it already
owns exactly this responsibility for iTerm2 (`is_iterm_installed`,
`install_iterm_via_brew`, `is_brew_available`). A generalized
multi-backend-aware version of `check_environment()` belongs here, not in
`install.sh` (kept thin/POSIX-sh by design) or scattered into `cli.py`.
`claudespace/config.py` is the correct home for registering a `"cmux"`
backend value; `claudespace/backends/` is the correct home for a `CmuxBackend`
implementation, per the ADR's own stated plan (`backends/cmux.py`, modeled on
`backends/iterm.py`, dispatched from `backends/__init__.py`) — this is
already fully specified in `docs/design/2026-09-03-cmux-backend-scoping-adr.md`,
not something to re-decide.

No `CLAUDE.md`/project-doc statement addresses this specific placement beyond
the ADR itself.

# Execution Flow

```
install.sh
    ↓ (unconditional, no backend arg)
claudespace doctor --yes --no-launch
    ↓
cli.py main() → environment.check_environment(assume_yes=True, launch=False)
    ↓
is_claude_installed()          (backend-agnostic)
is_iterm_installed()           (hardcoded, always runs)
    ↓ (if false)
install_iterm_via_brew()       (hardcoded brew cask install)
    ↓
_ensure_api_enabled()          (iTerm2-specific)
```

Separately, at real run time only (`claudespace` with no subcommand):

```
cli.py main() → _resolve_backend() → config.load_terminal_backend()
    ↓
get_backend() → ItermBackend | TmuxBackend
    ↓
_ensure_terminal_launched(backend)
    ↓ (skips entirely if TmuxBackend)
environment.ensure_environment(...)   # same iTerm2-only checks as above
```

The backend-aware path exists but only downstream of doctor/install; doctor
and `_ensure_terminal_launched` currently call the *same* iTerm2-only
`check_environment`/`ensure_environment` independently of any backend choice.

# Relevant Files

- `install.sh` — entry point that unconditionally invokes doctor; the "macOS
  only, drives iTerm2" framing and refusal-to-run gate live here.
- `claudespace/environment.py` — all current install/support detection logic
  (iTerm2-only today).
- `claudespace/cli.py` — doctor subcommand wiring, `_resolve_backend`,
  `_ensure_terminal_launched`, `_check_tmux_persistence` (existing precedent
  for a backend-conditional, non-fatal doctor check).
- `claudespace/config.py` — `KNOWN_TERMINAL_BACKENDS`, `load_terminal_backend`,
  `load_tmux_viewer` (ghostty is configured here, not detected here).
- `claudespace/backends/__init__.py` — `get_backend()` dispatch table.
- `claudespace/backends/tmux_cli.py` — `is_tmux_available()`, the one
  existing "is this backend usable" probe, currently under-used.
- `docs/design/2026-09-03-cmux-backend-scoping-adr.md` — authoritative,
  already-decided plan for adding cmux as a backend, gated on the spike below.
- `docs/research/2026-09-03-cmux-backend-spike.md` — the unrun go/no-go spike
  gating whether `CmuxBackend` gets built at all.

# Relevant Components

- Install script (`install.sh`)
- Environment/doctor checks (`claudespace/environment.py`)
- CLI backend resolution (`claudespace/cli.py`)
- Backend config/registry (`claudespace/config.py`,
  `claudespace/backends/__init__.py`)

# Existing Constraints

- `install.sh` refuses to run on non-Darwin (`install.sh:28-30`) — this is
  about the OS, not the terminal backend; unrelated to this request but a
  hard boundary already in place (see also the separate, gated
  `docs/design/2026-09-03-windows-support-psmux-scoping-adr.md`, out of scope
  here).
- `DEFAULT_TERMINAL_BACKEND = "iterm2"` and defaulting behaviour is explicitly
  documented as intentional (`config.py:33-38`: "Absent file or key defaults
  to iTerm2 (FR1/AC8), never a silent third option") — any redesign of
  install-time detection must not silently change the run-time default
  backend behind the user's back.
- `install_iterm_via_brew` already has a non-interactive path
  (`assume_yes`/`--yes`, required because `install.sh` runs `doctor --yes
  --no-launch` non-interactively) — any new backend-suggestion UX at install
  time must work non-interactively too (curl-pipe-sh has no TTY in the
  general case, though `install.sh` itself does prompt for other things when
  interactive).
- cmux backend addition is explicitly gated: the ADR states implementation
  should not start until the spike (`docs/research/2026-09-03-cmux-backend-
  spike.md`) passes its `[MUST]` checks (A0-A8, A10, B1, B3) — the spike's
  Results section is currently empty ("not yet run").

# Existing Behaviour

- `is_iterm_installed()` and `is_brew_available()` already handle the
  "checked hardcoded paths, then broadened via mdfind/well-known brew paths"
  pattern that any new `is_ghostty_installed()`/`is_cmux_installed()` should
  follow for consistency (per-app bundle ID via `mdfind`, well-known
  Homebrew-cask install paths).
- `_check_tmux_persistence` is the existing precedent for a non-fatal,
  backend-conditional doctor check that doesn't affect the overall exit code
  — the template a "check if tmux/ghostty/cmux is present, but don't force an
  install unless truly nothing is available" check would likely follow.

# Unknowns

- `[product]` Whether "detect what's supported and suggest installing
  something only if nothing is available" should change the *default*
  backend selection (currently always `iterm2` absent config), or purely
  change what doctor auto-installs/warns about while leaving
  `DEFAULT_TERMINAL_BACKEND` untouched. This is a product decision with
  real behavioural consequences (a user with only tmux+ghostty installed
  would otherwise still default to the iTerm2 backend and get bounced).
- `[product]` Priority order when multiple backends are usable (e.g. user has
  both iTerm2 and tmux, or — post-spike — cmux too): which does doctor
  suggest/prefer, and does that interact with `DEFAULT_TERMINAL_BACKEND`.
- `[product]` Whether cmux support should be built now (jumping ahead of the
  ADR's own gating) or the spike must run first, per the ADR's explicit
  sequencing ("Run the spike... If it passes: implement CmuxBackend"). The
  ADR and spike exist specifically to make this decision non-ad-hoc; I did
  not attempt to run the spike (out of scope for a research investigation —
  it requires a real cmux install and is Design's/Principal's/Implementer's
  call whether to execute it now).
- `[engineering - unresolved]` Exact `is_ghostty_installed()` detection
  method (bundle ID, binary path) — not yet defined anywhere in the repo;
  Ghostty is currently only referenced as `tmux`'s default *viewer* config
  string, never probed for presence. Resolvable during implementation, not a
  product question.

# Handoff note

This request changes user-facing install behaviour (what gets auto-installed
under what conditions) and includes a genuine open product question (whether
cmux should be built now, ahead of its own gated spike) — routing to
**planner**.
