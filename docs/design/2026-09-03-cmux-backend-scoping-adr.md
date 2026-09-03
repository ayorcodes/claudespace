# ADR: A cmux backend for macOS (scoping)

Status: **Proposed** — decision gated on the spike in
`docs/research/2026-09-03-cmux-backend-spike.md`. Nothing implemented.

Date: 2026-09-03

## Context

cmux (`github.com/manaflow-ai/cmux`) is a native **macOS 14+** terminal *app*
built on libghostty, purpose-built for running AI coding agents in parallel:
vertical tabs, split/browser panes, a per-pane sidebar (name, branch, cwd,
port, unread), and **notification rings when an agent stops and waits for
input**. It is open source and driven by a **JSON-RPC-over-Unix-socket API**
(`/tmp/cmux.sock` or `$CMUX_SOCKET_PATH`, `0600`, owner-checked) plus a
matching CLI.

This is squarely claudespace's use case, and the recent `TerminalBackend`
split (`backends/base.py`, iterm2/tmux backends) makes a third backend
additive. The question this ADR scopes: *should cmux become a macOS backend,
and what does it cost?*

**cmux is not a Windows answer.** It is macOS-only (an unofficial Linux port
exists); it does nothing for the psmux/Windows track
(`docs/design/2026-09-03-windows-support-psmux-scoping-adr.md`). It is a
*modern macOS backend* — a candidate to sit alongside, or eventually
supersede, the iTerm2 backend.

### How cmux differs from the tmux backend's assumptions

The tmux backend is a **headless** multiplexer (AD3: "no terminal needs to be
running"). cmux is the opposite shape: a **GUI app you drive**, exactly like
the iTerm2 backend, which also requires its app running and a scripting API.
So a `CmuxBackend` is modeled on `ItermBackend`, not `TmuxBackend`.

### The `TerminalBackend` contract vs cmux's socket API

| Backend method (concept) | cmux socket API |
|---|---|
| `build_workspace` | `workspace.create {cwd}` + `pane.create {direction, type:terminal}` per role |
| `send_role_prompt` | `surface.send_text {surface_id, text}` + `send-key enter` |
| readiness / submit-confirm / watchdog reads | `surface.read_text {surface_id, scrollback, lines}` |
| `find_role_pane` / `each_pane` | `pane.list` / `surface.list` |
| `activate_pane` | `pane.focus {pane_id}` |
| stable pane ids | `pane:N` / `surface:N` refs |

Content-read (`surface.read_text`) is the `capture-pane` equivalent — the
primitive zellij lacked — and it's present. Good.

## Decision (proposed, gated)

**Add a `CmuxBackend` (`backends/cmux.py`) over the JSON-RPC socket, modeled
on `ItermBackend`, contingent on the spike proving two things: targeted
`surface.read_text` works on an unfocused/background workspace, and pane
identity/state can be carried without an `@cs_*`-style store.** The bet is
that cmux is a clean macOS GUI backend whose only real design cost is
re-homing per-pane state.

Sequencing:

1. **Run the spike** (go/no-go) against a real cmux install.
2. **If it passes:** implement `CmuxBackend`, selectable via
   `config.toml [terminal] backend = "cmux"` (alongside `iterm2`/`tmux`),
   reusing the shared `backends/common.py` (launch command, prompt prefixes,
   timing constants) unchanged.
3. **If a must-pass fails:** do not build the backend; record cmux as
   "watch, revisit when the API closes the gap," and stay on iTerm2/tmux for
   macOS.

## The crux: per-pane state must be re-homed

The iTerm2 and tmux backends store identity as arbitrary per-pane key/value:
tmux `@cs_workspace/_role/_instance/_run_doc/_auto_handoff/_lazy/_template`,
iTerm2 session user-variables. **cmux's socket API exposes only fixed pane
fields (id, cwd, branch, title, ports, unread) — no arbitrary tag store.**
This is the same wall zellij hit. The re-homing plan:

- **One claudespace session ⇒ one cmux workspace.** Map the workspace's
  identity (marker + instance) onto the workspace name/title.
- **Role ⇒ pane title.** Encode `role` (and, if needed, `instance`) in each
  pane's title/name and parse it back from `pane.list`/`surface.list`.
- **Mutable run state ⇒ files.** `run_doc`, `auto_handoff`, `lazy`,
  `template`, and the pipeline batons already move to per-session files under
  the **per-session marker scoping** design
  (`docs/design/2026-09-03-per-session-marker-scoping.md`). That design and
  this one reinforce each other: with state file-homed, the missing pane KV
  store matters far less.

Consequence: a `CmuxBackend` is a **genuine new backend with a re-homed state
layer**, not a thin wrapper — but the file-based scoping does most of the
heavy lifting, so the backend itself stays close to `ItermBackend` in shape.

## Scope

**In scope**
- A go/no-go spike against the socket API (companion doc).
- If GO: `CmuxBackend` + config selection, reusing `backends/common.py`.
- State re-homing to workspace/pane titles + files (shared with the scoping
  design).

**Out of scope (explicitly)**
- Windows/Linux. cmux is macOS-only here; the unofficial Linux port is not a
  supported target.
- Browser panes, screenshots, and other cmux app features beyond what the
  five backend primitives need.
- Replacing the iTerm2 backend. cmux is added *alongside* it; any deprecation
  is a later, separate decision once cmux is proven in the pipeline.
- Adopting cmux's native "agent waiting" notification as the watchdog signal
  (see Consequences — noted as an opportunity, not committed).

## Consequences

**If it works out**
- A macOS backend purpose-built for multi-agent runs, reusing `common.py` and
  the whole pipeline/handoff layer unchanged.
- cmux's **"agent waiting" notification ring** is essentially the watchdog's
  stall signal as a first-class primitive; `surface.read_text` already covers
  content-diff. cmux could eventually give a *cleaner* stall signal than the
  iTerm2 screen-scrape — tracked as a follow-up, not this ADR.
- The per-session scoping design's file-homed state pays off twice.

**Risks accepted / to watch**
- **Re-homed state is load-bearing.** Encoding role/instance in titles is
  more fragile than a real KV store (a user renaming a tab could confuse
  discovery). The spike must confirm titles round-trip and that some field is
  reliably ours to own.
- **GUI-bound reads.** If `surface.read_text` only works on the *focused*
  surface/workspace, the whole multi-pane readiness/watchdog model breaks (the
  zellij-#4508 failure mode in GUI form). This is the top must-pass.
- **Young, single-vendor app + JSON-RPC surface** may change; a backend is
  more code to maintain than a tmux binary swap. Mitigated by isolation behind
  `TerminalBackend`.
- **macOS-14-plus floor** is higher than iTerm2's; some users can't run it.

## References

- Spike / go-no-go: `docs/research/2026-09-03-cmux-backend-spike.md`
- Per-session marker scoping (state re-homing depends on it): `docs/design/2026-09-03-per-session-marker-scoping.md`
- Backend interface & iTerm2 precedent: `backends/base.py`, `backends/iterm.py`, `docs/design/2026-09-02-ghostty-terminal-support.md`
- cmux: https://github.com/manaflow-ai/cmux · Socket API: https://manaflow-ai-cmux.mintlify.app/automation/socket-api · CLI: https://cmux.com/docs/api
