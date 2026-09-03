# ADR: Native Windows support via a psmux-compatible tmux backend (scoping)

Status: **Proposed** — decision gated on the spike in
`docs/research/2026-09-03-psmux-windows-spike.md`. Nothing implemented.

Date: 2026-09-03

## Context

claudespace is macOS-only. The entry point hard-gates on
`environment.require_macos()` (`cli.py`), the viewer launch is macOS
`open -b <bundle_id>` (`utils.launch_viewer`), and `themes.py` imports
`iterm2` at module scope. The recent **tmux backend** (`backends/tmux.py`,
`backends/tmux_cli.py`) removed the deepest coupling: its core is OS-agnostic
`asyncio.create_subprocess_exec("tmux", …)` on a dedicated `-L claudespace`
socket, with a `TerminalBackend` interface that already makes a third backend
additive rather than invasive.

The question this ADR scopes: *does the tmux backend let us support Windows,
and how?*

### The backend contract a Windows multiplexer must satisfy

From the tmux backend's design (AD3–AD6), the non-negotiable primitives are:

1. **Detached/headless server** — build and drive a session with no terminal
   attached (`new-session -d`); the viewer is optional chrome.
2. **Read pane content while detached** — `capture-pane -p -J`, feeding
   readiness polling, submit-confirmation, and the watchdog content-diff.
3. **Send text to a specific pane** — `send-keys -l --`, plus the
   `set-buffer`/`paste-buffer -p` path for large prompts.
4. **Arbitrary per-pane key/value state** — `set-option -p @cs_*` /
   `show-options -p -v @cs_*`, and `@cs_*` interpolation in `list-panes -F`.
   This holds workspace/role/instance/run-doc identity.
5. **Stable pane ids, server namespacing** — `#{pane_id}`, `-L <socket>`.

### Alternatives researched (2026-09-03)

Findings verified against vendor docs/issues, not memory. Confidence noted
because several are self-reported vendor claims, not things run locally.

- **tmux (native Windows): impossible.** POSIX-only; on Windows it runs only
  under WSL2/Cygwin/MSYS2. "tmux on Windows" is therefore Linux-under-WSL —
  the same work as native **Linux** support, which the tmux backend already
  most of the way delivers. *(High confidence.)*
- **Zellij 0.44.0 (Mar 2026): native Windows + CLI automation.** Has
  `attach --create-background` (headless), `action write-chars/send-keys
  --pane-id`, `list-panes --json`. **Two disqualifying gaps for our model:**
  `dump-screen` only works with a client **attached**
  (zellij-org/zellij#4508, open) — breaks the detached-read premise; and it
  has **no arbitrary per-pane KV store** — the `@cs_*` state model would have
  to be re-homed. A zellij backend is a genuine rewrite of the state and
  read-content layers. *(High confidence on the two gaps.)*
- **WezTerm: not the detached-server answer on Windows.** Great CLI
  (`get-text`, `send-text`, user-vars) and a native Windows GUI, but its
  **multiplexer server is not supported on Windows** (no named-pipe mux), and
  user-vars are not exposed in `cli list --json` (wezterm/wezterm#3675). Only
  ever a viewer for us, never the backend. *(High confidence.)*
- **psmux (`github.com/psmux/psmux`): native Windows, speaks the tmux command
  language.** Rust, ConPTY, Win10/11, packaged (winget/scoop/choco/cargo),
  MIT, ~3.4k stars, actively developed. Its docs claim the *entire* contract
  above — `-L`, detached `new-session -d`, `capture-pane`, **pane-scoped
  options**, `send-keys -l --`, named `set-buffer`/`paste-buffer` — and it
  explicitly advertises "first-class support for Claude Code agent teams."
  *(Compatibility claims are **self-reported vendor docs**, not verified
  locally; the "Trusted by …" astroturf copy is in forks, not the canonical
  repo.)*

## Decision (proposed, gated)

**Pursue native Windows as a psmux-backed reuse of the existing tmux backend
— not a new backend — contingent on the spike proving psmux's tmux-CLI
fidelity.** The bet is that Windows support costs a *binary swap plus OS
plumbing*, not a rewrite, because psmux implements the same CLI our
`tmux_cli.py` already speaks.

Sequencing:

1. **Linux native first** (prerequisite, low cost): make `require_macos()`
   backend-aware (tmux backend needs only `tmux` on PATH; iterm2 backend needs
   macOS), make `launch_viewer` pluggable per-OS, and decouple `themes` from
   the `iterm2` import. This is independently valuable and de-risks Windows.
2. **Run the spike** (go/no-go) against psmux on Win10/11.
3. **If the spike passes:** add psmux as a selectable binary/viewer for the
   existing tmux backend (config: `[terminal.tmux] binary`, Windows viewer via
   `wt.exe`/psmux attach). No new `TerminalBackend`.
4. **If it fails a must-pass:** fall back to documenting **WSL2 + tmux +
   Windows Terminal** as the supported Windows path, and close native Windows
   as out of scope until psmux (or another native multiplexer) closes the gap.

## Scope

**In scope**
- A go/no-go spike defining the exact compatibility contract (companion doc).
- Backend-aware entry gating, pluggable viewer, `themes`/`iterm2` decoupling
  (the Linux-native prerequisite).
- psmux selection as a tmux-binary swap **if** the spike passes.

**Out of scope (explicitly)**
- Any new `TerminalBackend` subclass. If psmux needs one, that is a *separate*
  ADR — it would mean psmux is not CLI-faithful and the core bet failed.
- **Reboot persistence on Windows.** Increment 2 persistence is
  tmux-resurrect/tmux-continuum (shell plugins); they do not port to psmux.
  Windows ships **without** persistence initially; a native mechanism is a
  later, separate decision.
- Rewriting the state or read-content layer for a non-tmux-CLI multiplexer
  (i.e. the zellij route). Not chosen.

## Consequences

**If it works out**
- Windows support reuses ~all of `tmux_cli.py`/`TmuxBackend`, its 48 headless
  integration tests become the Windows conformance suite, and the paste-buffer
  fix and per-session scoping designs carry over unchanged.
- Three viewers to reason about (iTerm2 / Ghostty+Linux terminals / Windows
  Terminal), one backend family.

**Risks accepted / to watch**
- **Self-reported compatibility.** The whole bet rests on psmux fidelity for
  detached `capture-pane -p -J` and `@cs_*` option round-trip — the two things
  that broke zellij. The spike exists to convert this from claim to evidence
  before any code.
- **Third-party binary in a safety path.** The watchdog trusts `capture-pane`
  fidelity for stall detection, and psmux would alias/replace `tmux` on the
  user's PATH. Vet provenance and pin a version before it's load-bearing.
- **Maintenance/abandonment risk** of a young project; mitigated by the fact
  that a psmux swap adds ~no claudespace code to unwind if it's dropped.
- **No Windows persistence** at first — a real feature gap vs macOS/Linux,
  called out above.

## References

- Spike / go-no-go: `docs/research/2026-09-03-psmux-windows-spike.md`
- Per-session marker scoping (carries over): `docs/design/2026-09-03-per-session-marker-scoping.md`
- tmux backend design: `docs/design/2026-09-02-ghostty-terminal-support.md`
- psmux: https://github.com/psmux/psmux · https://github.com/psmux/psmux/blob/master/docs/compatibility.md
- Zellij detached dump-screen: https://github.com/zellij-org/zellij/issues/4508
- WezTerm CLI user-vars: https://github.com/wezterm/wezterm/issues/3675
