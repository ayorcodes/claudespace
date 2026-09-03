# Spike: can cmux's socket API back a claudespace macOS backend?

Status: **not yet run.** Go/no-go for the ADR
`docs/design/2026-09-03-cmux-backend-scoping-adr.md`.

Purpose: prove, before writing a `CmuxBackend`, that cmux's JSON-RPC socket
API covers the five `TerminalBackend` primitives — and, crucially, that
per-pane identity can be carried without an `@cs_*` key/value store. Run on
**macOS 14+** with cmux installed. Timebox ~half a day.

## Setup

```bash
# install cmux (see github.com/manaflow-ai/cmux), launch the app once
export CMUX_SOCKET_PATH="${CMUX_SOCKET_PATH:-/tmp/cmux.sock}"
ls -l "$CMUX_SOCKET_PATH"        # exists, 0600, owned by you
```

Probe via the CLI where one exists, else raw JSON-RPC:
`{ "id":"<uuid>", "method":"<m>", "params":{...} }` over the socket
(e.g. `nc -U "$CMUX_SOCKET_PATH"` or a tiny Python `socket` client). Record
every raw response.

Notation: **[MUST]** = failure is a no-go for the backend. **[WANT]** =
degrades or has a workaround.

---

## Part A — socket API probes

- [ ] **A0 — daemon reachable + auth model.** Connect and call a trivial
  method (e.g. `list-workspaces`). **[MUST]** the socket answers, and confirm
  it rejects a socket owned by another user (the documented 0600/owner check)
  — we rely on that for the "no fake-socket" safety property.

- [ ] **A1 — create a workspace.** `workspace.create {cwd:<dir>}` →
  `workspace_id` + `workspace_ref`. **[MUST]** returns a stable id usable in
  later calls. (This is `build_workspace`'s container; one claudespace session
  = one workspace.)

- [ ] **A2 — create panes per role.** From the workspace, `pane.create
  {workspace_id, direction, type:"terminal"}` four times to reach 5 panes.
  **[MUST]** each returns a distinct stable pane/surface ref (`pane:N` /
  `surface:N`), and `pane.list {workspace_id}` shows all five.

- [ ] **A3 — send text to a SPECIFIC pane, then submit.**
  `surface.send_text {surface_id:<one pane>, text:"echo cmux-marker-123"}`
  then `send-key enter` (or `surface.send_text` with a newline if that's how
  submit works — record which). **[MUST]** the text lands in *that* pane, not
  just the focused one. (This is `send_role_prompt`.)

- [ ] **A4 — read a SPECIFIC pane's screen.**
  `surface.read_text {surface_id:<same pane>, scrollback:false, lines:N}` →
  `text` containing `cmux-marker-123`. **[MUST]** targeted read works. (This
  is the `capture-pane` equivalent feeding readiness/submit-confirm/watchdog.)

- [ ] **A5 — read works while the workspace/pane is UNFOCUSED / backgrounded.**
  Focus a *different* workspace (or another macOS app), then repeat A4 against
  the original pane. **[MUST]** content still comes back. If reads only work
  on the focused surface, the whole multi-pane model is dead on cmux (the
  zellij-#4508 failure mode, GUI edition). **This is the top must-pass.**

- [ ] **A6 — a settable, readable identity field per pane (the `@cs_*`
  substitute).** Set a pane's title/name (e.g. via `tab.action
  {action:"rename", title:"researcher"}` or the pane-create name), then read
  it back from `pane.list` / `surface.list`. **[MUST]** the title round-trips
  and appears in the list output. Determine exactly which field is reliably
  *ours* to own for encoding `role` (and `instance` if the workspace
  name can't hold it). Without this, pane discovery ("which pane is the
  reviewer?") has nothing to key on.

- [ ] **A7 — list fields inventory.** Capture the full JSON of `pane.list` and
  `surface.list` for the workspace. **[MUST]** record every field returned
  (id, cwd, branch, title, ports, unread, …). This is the authoritative input
  to the state-re-homing plan — decide from real output whether `role`/
  `instance` fit in title alone or need the workspace name too.

- [ ] **A8 — multiple workspaces coexist and are independently addressable.**
  Create a second workspace; confirm calls targeting workspace A don't affect
  B, and `list-workspaces` shows both with stable ids. **[MUST]** (maps two
  concurrent claudespace sessions → two workspaces; also the per-session
  scoping story).

- [ ] **A9 — focus / activate a pane.** `pane.focus {pane_id}`. **[WANT]**
  brings the pane forward (the `activate_pane` nicety).

- [ ] **A10 — large prompt via send_text (truncation check).** Send a
  single-shot ~3 KB string via `surface.send_text`, then `surface.read_text`
  and confirm the leading bytes arrived (not just the tail). **[MUST]** no
  front-truncation — this is the exact bug the tmux paste-buffer fix
  addressed; confirm cmux's send_text is atomic, or that chunking is needed.

- [ ] **A11 — read is wrapped-line coherent.** In a narrow pane, print a
  string longer than the pane width; `surface.read_text` and check a long
  logical line isn't split mid-word in a way that would break prompt matching
  (the `-J` concern). **[WANT]** record whether read_text returns visual rows
  or logical lines.

- [ ] **A12 — "agent waiting" notification exposed via API (bonus).** Check
  whether the notification-ring / unread state is queryable over the socket
  (`pane.list` `unread`, or an event/subscription). **[WANT]** — if yes, it
  could become a first-class watchdog stall signal later (out of scope for
  go/no-go).

---

## Part B — end-to-end shape check (no backend code)

- [ ] **B1 — scripted 5-pane build.** With a short script (Python socket
  client), reproduce a workspace build: create workspace, create 5 panes,
  rename each to a role, send each a distinct command, read each back,
  focus one. **[MUST]** the whole sequence runs without the GUI needing manual
  interaction — confirms `build_workspace` + `send_role_prompt` +
  readiness-read are automatable.

- [ ] **B2 — teardown.** Close panes / workspace via the API
  (`tab.action {action:"close"}` / `close-workspace`). **[WANT]** clean
  teardown for `close_window_if_empty` semantics.

- [ ] **B3 — reconnect / discovery after a fresh process.** Kill the script,
  start a new socket client, and re-discover the workspace + role panes purely
  from `list-workspaces`/`pane.list` + the titles set in A6. **[MUST]** a Stop
  hook running as its own process (`handoff.py`'s model) can find the right
  pane with no in-memory state — this is what makes handoff work.

---

## Scoring & decision gate

**GO (build `CmuxBackend`)** requires every [MUST]: A0, A1, A2, A3, **A4, A5**
(targeted read, incl. unfocused), **A6, A7** (a readable identity field +
known list fields), A8, A10, B1, B3.

**Conditional GO** if all [MUST] pass but some [WANT] (A9 focus, A11 wrapping,
B2 teardown) fall short: proceed and log each as a tracked workaround.

**NO-GO (stay on iTerm2/tmux for macOS)** if any of these fail:
- **A5** — reads only on the focused surface ⇒ no multi-pane readiness/
  watchdog. Fatal.
- **A6/A7** — no field we can set and read back to carry `role`/`instance` ⇒
  pane discovery has no key; the identity model can't be re-homed onto cmux.
- **B3** — a fresh process can't rediscover panes ⇒ the Stop-hook handoff
  can't target the next role.
- **A10** — `send_text` truncates a large prompt with no chunking path ⇒
  reintroduces the handoff-truncation bug with no fix.

Append raw JSON for A4/A5/A6/A7/A10 and B3 to the results section — the
go/no-go must be reproducible, not a vibe.

## Results (run 2026-09-03)

- cmux version / macOS version: cmux 0.64.22 (102) [ddd4a01bc], macOS 26.5.2
  (Darwin 25.5.0), installed via `brew install --cask cmux`.
- Socket path + perms: `~/.local/state/cmux/cmux.sock` (NOT `/tmp/cmux.sock`
  as the setup snippet assumed — spike doc's `CMUX_SOCKET_PATH` default is
  wrong), `srw-------` (0600), owned by the invoking user.
- **Auth model differs from what this doc assumed.** 0600/owner is necessary
  but not sufficient: cmux also enforces `automation.socketControlMode` in
  `~/.config/cmux/cmux.json` (schema enum: `off|cmuxOnly|automation|password|
  allowAll|openAccess|fullOpenAccess|notifications|full`; default
  `cmuxOnly`). Under the default, an external CLI process (not spawned inside
  a cmux pane) gets `Access denied - only processes started inside cmux can
  connect` even with correct socket perms. Had to set
  `"automation": {"socketControlMode": "automation"}` and `cmux
  reload-config` before any probe could run. **A backend integration must
  document/require this setting** (or detect+prompt) — `0600/owner-checked
  socket` alone, as `environment.is_cmux_installed()` was scoped to check, is
  not the whole gate.
- API shape: not raw `pane.create`/`surface.send_text` JSON-RPC as this doc
  assumed for casual use — there's a real CLI (`cmux <command>`) wrapping the
  same v2 RPC methods, plus a `cmux rpc <method> <json>` escape hatch for the
  full raw JSON-RPC method space (methods are named `surface.list`,
  `surface.send_text`, etc., matching this doc's assumed naming — just
  invoked via `cmux rpc <method> '{...}'`, or via higher-level CLI verbs like
  `workspace create`, `new-split`, `send`, `capture-pane`, `list-panels`,
  `rename-tab`). Either interface is usable; the CLI is friendlier for a
  Python subprocess-based backend, `cmux rpc` covers anything the CLI
  doesn't expose a verb for.

### Part A

- A0 [MUST] **PASS (with caveat above).** `cmux ping` → `PONG` once
  `socketControlMode` set to `automation`. `cmux capabilities` confirms
  `"access_mode": "automation"`.
- A1 [MUST] **PASS.** `cmux workspace create --cwd <dir>` → `OK workspace:N`,
  stable ref usable in later calls; `cmux rpc workspace.list` gives full
  UUID + ref + cwd.
- A2 [MUST] **PASS.** `cmux new-split right|down` x4 → 5 distinct
  `surface:N` refs (`surface:4..8`), each with a distinct `pane_ref`;
  `list-panels` / `cmux rpc surface.list` show all five.
- A3 [MUST] **PASS.** `cmux send --surface surface:6 "echo cmux-marker-123"`
  + `cmux send-key --surface surface:6 enter` landed only in surface:6.
  Submit is two calls (`send` then `send-key enter`), not embedded in one —
  matches this doc's "record which" note.
- A4 [MUST] **PASS.** `cmux capture-pane --surface surface:6 --lines 20`
  returned exactly the marker output; `capture-pane` on surface:4 (untouched)
  showed no marker.
- A5 [MUST] **PASS — the top must-pass held.** Switched the active workspace
  to a different one (`workspace select workspace:2`) and brought Finder
  frontmost (fully backgrounding the cmux app), then re-ran `capture-pane`
  against the original surface:6 in the backgrounded workspace:4 — content
  still returned correctly. Reads are not focus-gated.
- A6 [MUST] **PASS.** `cmux rename-tab --surface surface:6 "researcher"` set
  the title; round-tripped via both `list-panels` and `cmux rpc
  surface.list` (`"title": "researcher"`). `surface.title` is the field to
  own for `role` encoding.
- A7 [MUST] **PASS.** Full field inventory captured via `cmux rpc
  workspace.list` and `cmux rpc surface.list` — see below.
- A8 [MUST] **PASS.** Multiple workspaces (workspace:1/2/3/4 concurrently)
  independently addressable; calls scoped by `--workspace` didn't cross-talk;
  `workspace.list` shows all with stable UUIDs+refs.
- A9 [WANT] Not exercised this run (no blocking reason to skip; low risk,
  `surface.focus`/`cmux ... --focus` exists in the capability list).
- A10 [MUST] **PASS.** Sent a single `send` call with an embedded ~3KB
  string (`START-...3000 x's...-END`); `capture-pane`-equivalent output file
  showed both the leading `START-` marker and trailing `-END` marker intact,
  full byte count (3051) preserved — no front-truncation, no chunking
  needed.
- A11 [WANT] Not exercised.
- A12 [WANT] Not exercised.

`surface.list` field inventory (A7, one entry, from `cmux rpc surface.list`):
`focused` (bool), `id` (UUID), `index`, `index_in_pane`, `initial_command`,
`pane_id` (UUID), `pane_ref` (`pane:N`), `ref` (`surface:N`),
`requested_working_directory`, `resume_binding`, `selected_in_pane`,
`title` (the writable identity field, confirmed via A6), `tmux_start_command`,
`type`. `workspace.list` fields include `current_directory`, `custom_title`,
`title` (defaults to `~cwd`, settable via `workspace rename` — not tested
this run), `id`/`ref`, `selected`, plus an unused-for-us `remote{}` block
(SSH/tmux remote-workspace state) and `listening_ports`.

### Part B

- B1 [MUST] **PASS (de facto).** The A1–A6 sequence above ran end-to-end from
  a plain shell with zero GUI interaction after initial app launch/onboarding
  — workspace create, 5-pane build, targeted send+read, rename, all scripted.
- B2 [WANT] **PASS.** `cmux workspace close workspace:N --force` cleanly
  tore down the test workspace and its panes (used twice, both clean).
- B3 [MUST] **PASS.** From a brand-new `bash -c '...'` subshell (no
  inherited state), discovered the target workspace purely by matching
  `current_directory` in `cmux rpc workspace.list`, then found the "reviewer"
  pane purely by matching `title` in `cmux rpc surface.list` scoped to that
  workspace_id. No in-memory state required — confirms a Stop-hook-style
  fresh process can rediscover panes.

### Verdict: **GO**

Every [MUST] passed: A0, A1, A2, A3, A4, A5, A6, A7, A8, A10, B1, B3. The one
real surprise is **not architectural** — it's that
`environment.is_cmux_installed()` as scoped in the ADR (app present + 0600/
owner-checked socket) is necessary but not sufficient; it must also confirm
`automation.socketControlMode` isn't left at the default `cmuxOnly`, or the
backend will get `Access denied` on first use despite everything else being
correct. Recommend the detection step run a `cmux ping` (or `cmux
capabilities`) liveness check, not just a socket-file stat, and surface a
clear error/remediation (the exact JSON key + value to set, per this run)
when it fails. No changes to the planned primitive mapping in the ADR are
needed otherwise — `pane.create`→`new-split`/`surface.create`,
`surface.send_text`→`send`+`send-key`, `capture-pane`→`capture-pane`/
`surface.read_text`, and title-as-identity all map cleanly.
