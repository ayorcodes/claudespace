# CmuxBackend — Implementation Design

Status: proposed (ready to implement).

## References

- Scoping ADR (the *what* and the go/no-go bet): `docs/design/2026-09-03-cmux-backend-scoping-adr.md`.
- Spike, **GO** verdict + raw field inventory: `docs/research/2026-09-03-cmux-backend-spike.md`.
- Socket-auth finding: `docs/research/2026-09-03-cmux-socket-auth-note.md`.
- Per-session marker scoping (the file-homed state this design leans on):
  `docs/design/2026-09-03-per-session-marker-scoping.md`.
- Interface + precedent: `claudespace/backends/base.py`, `iterm.py` (the GUI
  model this follows), `tmux.py` (the CLI-subprocess model this follows),
  `common.py` (reused verbatim), `backends/__init__.py`, `config.py`,
  `environment.py`, `layouts.py`.

This document assumes the ADR and spike are open. It covers the *how*.

---

# Architecture Decisions

### D1 — A CLI-subprocess backend: `backends/cmux.py` + `backends/cmux_cli.py`

`CmuxBackend` is a GUI-app backend (modeled on `ItermBackend`, per the ADR)
but driven the way `TmuxBackend` is: through a CLI, not an in-process library.
Split exactly as tmux is — a thin subprocess boundary (`cmux_cli.py`, the only
module that spawns `cmux`) under a backend (`cmux.py`) built from those
primitives. This gives the same test seam tmux has (fake runner) and the same
argv-not-shell safety property.

- **Reasoning:** the spike proved every primitive through the `cmux` CLI (and
  `cmux rpc <method>` for anything without a verb). A subprocess boundary
  mirrors `tmux_cli.py` one-to-one, so the readiness/submit/stall/paste logic
  and its tests carry over structurally.
- **Rejected — raw JSON-RPC socket client in Python.** More code (framing,
  ids, socket lifecycle) for no capability the CLI lacks; the spike confirmed
  the CLI covers all [MUST] primitives. Kept only as the `cmux rpc` escape
  hatch for reads the CLI has no verb for (`surface.list`/`workspace.list`
  JSON).
- **Rejected — extend `ItermBackend`.** Different automation surface entirely;
  the ADR already settled that cmux is a *new* backend, not a wrapper.

### D2 — Identity is carried on `surface.title`, the one field the spike proved writable

The spike confirmed **only** `surface.title` round-trips through set→list (A6);
workspace `title`/`custom_title` writability was *not* tested. So this backend
puts everything it must persist-and-rediscover onto `surface.title` and depends
on nothing unproven.

Each pane's title encodes **both** instance and role:

```
cs:<instance8>:<role>
```

(`instance8` = first 8 hex of the workspace UUID, matching the tmux session-name
convention `cs-<hash>-<instance8>`.) This is the `@cs_*` / iTerm2-user-variable
substitute for the two fields discovery keys on — `@cs_instance` and `@cs_role`.

- **Workspace container discovery** piggybacks on this: "the workspace for
  instance I" = whichever cmux workspace contains a surface titled
  `cs:I8:*`. No workspace-level tag needed.
- **`find_workspace(marker)` (no instance — the attach-or-build dedup)** keys on
  the workspace's `current_directory` matching `marker` (the resolved root),
  the exact match the spike's B3 used successfully, then reads that workspace's
  instance back out of any `cs:*:*` surface title it holds.
- **Reasoning:** surface title is the only proven-durable writable field; using
  it for both keys avoids a second, unproven identity mechanism.
- **Rejected — encode instance in `workspace.title`/`custom_title`.** Writability
  unproven by the spike (explicitly "not tested this run"). Designing on it
  would be speculation; if a later probe proves it, it becomes a cheap
  optimization (one workspace tag instead of scanning surface titles), not a
  correctness dependency.
- **Risk (from the ADR, accepted):** a user manually renaming a pane tab
  destroys that pane's identity. Same fragility the ADR flagged; unavoidable
  given no arbitrary KV store. `--name`-driven titles are set at launch and
  not normally user-touched.

### D3 — Mutable workspace state is file-homed, not stored in cmux

cmux exposes no arbitrary per-pane/-workspace KV store (the ADR's central
finding). The five mutable workspace fields the interface reads —
`auto_handoff`, `lazy`, `template`, `run_doc`, `run_started` — are **not** put
on cmux at all. They live in one JSON file per session:

```
<session_marker_dir(marker, instance)>/workspace-state.json
  = <root>/.claudespace/s/<instance>/workspace-state.json
```

`session_marker_dir` (already shipped by per-session marker scoping) is the
exact directory every marker for this session already uses, and `marker` *is*
the resolved root, so the path is a pure function of the `(marker, instance)`
every getter/setter already receives.

- **Reasoning:** the per-session-scoping design already file-homes the pipeline
  batons; co-locating workspace state there means a fresh Stop-hook process
  rediscovers it with zero cmux calls, and `get_run_doc`'s `(doc, started)` —
  read on every handoff — becomes a local file read instead of a socket round
  trip. This is the ADR's "mutable run state ⇒ files," made concrete.
- **Rejected — stuff a serialized blob into a surface title.** Titles are
  visible UI; overloading them with JSON is ugly and length-bounded, and the
  spike only validated a short human-ish string.
- **Instance-less reads:** `get_*` may be called with `instance=None` only from
  `workspace.py`'s attach-or-build probe, which is immediately paired with
  `find_workspace` (which *returns* the instance). When `instance is None`,
  resolve it first via `find_workspace(marker)`; if still unknown, treat state
  as absent (`False`/`None`) — identical to the "no workspace found" answer the
  other backends give.

### D4 — Detection gates on a `cmux ping` liveness check, not a socket stat (spike's one real surprise)

The spike proved `is_cmux_installed()` as the ADR scoped it (app present +
0600/owner socket) is **necessary but not sufficient**: under cmux's default
`automation.socketControlMode = "cmuxOnly"`, an external process gets
`Access denied - only processes started inside cmux can connect` despite a
correct socket. Detection must run a real liveness probe and, on failure,
name the exact remediation.

- `environment.is_cmux_reachable()` runs `cmux ping` (expect `PONG`) — or
  `cmux capabilities` (expect `"access_mode"` ∈ the non-`cmuxOnly` set) — with
  a short timeout.
- On the specific `Access denied` failure, the message names the fix verbatim:
  set `"automation": {"socketControlMode": "automation"}` in
  `~/.config/cmux/cmux.json`, then `cmux reload-config`.
- **Reasoning:** without this the backend's *first real call* fails opaquely
  after the workspace is half-built. Fail fast in `run()`'s preflight with an
  actionable message (the `BackendUnavailableError`/`sys.exit(1)` contract in
  `base.py`), exactly as tmux's version/availability preflight does.

### D5 — `config.py`/`get_backend`/`detect_usable_backends` gain a third value, iTerm2 stays the default

`"cmux"` joins `KNOWN_TERMINAL_BACKENDS`; `DEFAULT_TERMINAL_BACKEND` is
unchanged (`iterm2`). Selection is `config.toml [terminal] backend = "cmux"` or
`CLAUDESPACE_TERMINAL=cmux`, per the ADR. Additive; no existing behavior moves.

---

# Components

New:

- **`claudespace/backends/cmux_cli.py`** — the only module that spawns `cmux`.
  argv-list subprocess wrappers (never a shell string), mirroring
  `tmux_cli.py`: `ping`, `capabilities`, `workspace_create`, `new_split`,
  `send_text`, `send_key`, `capture_pane`, `rename_surface`, `focus_surface`,
  `workspace_list`, `surface_list`, `workspace_close`. Reads that need
  structured data go through `cmux rpc workspace.list` / `surface.list`
  (JSON); actions use the friendly verbs (`workspace create`, `new-split`,
  `send`, `send-key`, `capture-pane`, `rename-tab`).
- **`claudespace/backends/cmux.py`** — `CmuxBackend(TerminalBackend)`,
  `BACKEND_NAME = "cmux"`, plus `CmuxWindow`/`CmuxPane` opaque handles
  (`CmuxWindow(workspace_ref, instance)`, `CmuxPane(surface_ref, workspace_ref)`).
- **`workspace-state.json` writer/reader** — small pure helpers (in `cmux.py`
  or a `cmux_state.py`) over the file in D3.

Changed:

- **`claudespace/config.py`** — add `"cmux"` to `KNOWN_TERMINAL_BACKENDS`.
- **`claudespace/backends/__init__.py::get_backend`** — a `resolved == "cmux"`
  branch returning `CmuxBackend()`.
- **`claudespace/environment.py`** — `is_cmux_installed()` (app present, by
  bundle id via the existing `_app_installed`) + `is_cmux_reachable()` (the
  D4 ping); `detect_usable_backends()` appends `"cmux"` when both hold.
- **`claudespace/utils.py`** — cmux bundle id constant (for `_app_installed`),
  alongside the existing `GHOSTTY_BUNDLE_ID`/`VIEWER_BUNDLE_IDS`.

Reused unchanged: `backends/common.py` (all of `launch_command_text`,
`command_with_baked_persona`, `role_prompt_prefix`, `screen_signature`,
`stall_decision`, every timing constant), `layouts.py`, `pipeline.py`
(`session_marker_dir`, `resolve_root`, `think_marker_path`), `base.py`
interface, and the whole handoff/watchdog/workspace layer.

---

# Data Flow

**Build (`build_workspace`, eager):**

1. `cmux workspace create --cwd <resolve_root(root)>` → `workspace:N` ref
   (+ UUID via `workspace.list`). Mint `instance = uuid4()`.
2. Layout tree drives `split_pane` (→ `cmux new-split`) off the root surface to
   produce one surface per role (see D-split below).
3. Per pane: `rename-tab --surface <ref> "cs:<instance8>:<role>"`; then
   `launch_command_text(...)` (identical string both other backends send) via
   `send` + `send-key enter`.
4. Write `workspace-state.json` (`auto_handoff`, `lazy`, `template`, empty
   `run_doc`/`run_started`) under `session_marker_dir(marker, instance)`.
5. `_prefill_role_command` per pane (only for roles with no baked prompt file).
6. Return `CmuxWindow(workspace_ref, instance)`.

Lazy build: only the entry role's surface is created/tagged/launched; state
file still written; other panes appear on first handoff via `reveal_role`.

**Handoff (fresh `handoff.py` process, in-pane):** reads `CLAUDESPACE_ROOT`
(= marker), `INSTANCE`, `ROLE`, `TERMINAL=cmux` from env → `get_backend("cmux")`
→ `find_role_pane(marker, dest_role, instance)`:

1. `workspace.list` → find the workspace whose `current_directory == marker`
   **and** which holds a surface titled `cs:<instance8>:*` (instance is
   authoritative; current_directory disambiguates nothing further once instance
   matches).
2. `surface.list` for that workspace → the surface titled
   `cs:<instance8>:<dest_role>` → `CmuxPane`.
3. `send_role_prompt` types the handoff text and confirms submit
   (`capture-pane` diff, `common` timing).

`get_auto_handoff`/`get_lazy`/`get_template`/`get_run_doc` on that path read
`workspace-state.json` — no cmux call.

**Watchdog:** `each_pane(marker, instance)` lists the workspace's surfaces,
parses `cs:<instance8>:<role>` titles → `(role, pane)`; `check_pane_stall`
feeds `capture-pane` text through the shared `stall_decision`.

---

# The primitive mapping (authoritative)

| `TerminalBackend` method | cmux invocation |
|---|---|
| `build_workspace` container | `cmux workspace create --cwd <dir>` |
| `split_pane(pane, vertical)` | `cmux new-split <right\|down> --surface <src_ref>` (see D-split) |
| tag identity | `cmux rename-tab --surface <ref> "cs:<inst8>:<role>"` |
| `send_role_prompt` type | `cmux send --surface <ref> <text>` |
| `send_role_prompt` submit | `cmux send-key --surface <ref> enter` |
| readiness / submit-confirm / stall read | `cmux capture-pane --surface <ref> --lines N` |
| `find_role_pane`/`each_pane`/`find_workspace` | `cmux rpc workspace.list` + `cmux rpc surface.list` (JSON) |
| `activate_pane` | `cmux <focus verb> --surface <ref>` (WANT; best-effort) |
| `close_window_if_empty` | n/a — no cold-launch stray window (like tmux, a no-op) |
| workspace teardown (not in interface) | `cmux workspace close <ref> --force` |

**D-split — split targeting.** The layout tree needs `split_pane` to split a
*specific* source surface and return the new one. The spike proved `new-split`
creates panes and returns distinct `surface:N` refs, but did not confirm a
`--surface` target selector. **Decision:** call `new-split <dir> --surface
<src_ref>` and read the returned ref. **Fallback if `new-split` only splits the
active surface:** `focus_surface(src_ref)` first (focus is in cmux's capability
list, A9), then `new-split`. Either way `split_pane` returns the new
`surface:N`. This is the one primitive to confirm first at implementation
(Open Questions Q1); both paths are specified so implementer never redesigns.

**Send atomicity (A10 — no chunking needed).** The spike sent ~3 KB in one
`send` with both ends intact, so `send_role_prompt` uses a single `send` call
(no tmux-style paste-buffer framing). This is the direct analog of iTerm2's
single `async_send_text` write; keep the type-then-`send-key enter` split and
the `_confirm_submitted` retry loop (`SUBMIT_*` constants) unchanged.

---

# API / Database Changes

None (no HTTP API, no DB). The `workspace-state.json` schema:

```json
{"auto_handoff": true, "lazy": false, "template": "default",
 "run_doc": null, "run_started": null}
```

Written whole on build; `run_doc`/`run_started` merged in on `set_run_doc`.

---

# Validation

- `is_cmux_reachable()` must positively confirm `PONG`/`access_mode` — never
  infer reachability from a socket-file stat (D4).
- Surface-title parse: only titles matching `^cs:<8hex>:<role>$` are treated as
  ours; anything else (user-renamed, foreign workspace) is ignored, so a
  malformed title degrades to "pane not found," never a crash or misroute.
- `instance` is an internally minted UUID; no external validation.

# Error Handling

- Every `cmux_cli` call: argv list, bounded timeout, non-zero → `CmuxCommandError`
  (stderr carried), timeout → `BackendUnavailableError` — same shape as
  `tmux_cli.run`.
- `run()` preflight: not-macOS, `cmux` not on PATH, and **`Access denied`**
  each `sys.exit(1)` with a specific message (the D4 remediation for the last).
- Best-effort, never fatal: `rename-tab`, focus, `workspace close` (mirrors
  tmux's cosmetic-failure tolerance).
- Read helpers (`workspace.list`/`surface.list`) return `[]`/`None` on error so
  a lookup degrades to "not found," matching both existing backends.

# Security Considerations

- **argv, never shell** in `cmux_cli.py` — prompt text/paths/role names can't be
  reparsed as flags or injected (same guarantee as `tmux_cli`; guard leading
  `-` where cmux would misread it).
- The D4 remediation *widens* cmux's socket access mode
  (`cmuxOnly`→`automation`). The design **documents** it as a required user
  action and never edits `~/.config/cmux/cmux.json` itself — it's outside the
  repo and a user-owned security setting (the socket-auth note already treats it
  as user/implementer-applied). Detection prompts; it does not auto-widen.
- `guard.py` unaffected (state file lives under `/.claudespace/…`, still matched).

# Performance Considerations

- Discovery is **two** `cmux rpc` reads per lookup: one `workspace.list`, then
  one `surface.list` scoped to the matched workspace — O(1) round trips, not
  O(panes). Parse titles in-process. No N+1.
- `get_run_doc` and the other state getters are **local file reads** (D3), off
  the socket entirely — cheaper than tmux's per-lookup `list-panes`.
- `surface.list` scoping: if cmux exposes an all-workspace surface list, one
  read replaces the per-workspace loop — an optimization, not required (Q2).
- Watchdog `capture-pane` is one call per pane per poll, same cadence as tmux.

# Compatibility

- **Purely additive** (D5): iTerm2 remains default; iTerm2/tmux code untouched.
  A workspace built on cmux exports `CLAUDESPACE_TERMINAL=cmux`, so its own
  handoffs resolve back to cmux (the `launch_command_text` mechanism that
  already fixes this for `--tmux`).
- No migration; no on-disk format shared with other backends.
- macOS 14+ floor (cmux's), higher than iTerm2's — a `run()` preflight concern,
  surfaced as a clear message, not a silent failure.

# Edge Cases

- **`socketControlMode` left at default** → `Access denied`; caught in preflight
  with the exact-fix message (D4). The motivating spike surprise.
- **Two sessions, same root** → distinct instances → distinct `cs:<inst8>:*`
  titles and distinct `s/<instance>/` state files; no collision. `find_workspace`
  (no instance) returns the first current_directory match — acceptable for the
  attach-or-build probe, which then carries the returned instance forward.
- **User renames a pane tab** → that pane's identity is lost (accepted ADR
  risk); discovery reports "not found," handoff surfaces the normal missing-pane
  path rather than misrouting.
- **Lazy reveal** → `reveal_role` splits the largest sibling (dims via
  `surface.list`/`capture` geometry or a `display`-style call), tags+launches,
  reads `think` from `think_marker_path(root, instance)` (file, same as tmux).
- **`current_directory` drift** (a role `cd`s away) → only affects the
  instance-less `find_workspace` fallback; the instance-keyed paths (every
  handoff/watchdog call) don't consult `current_directory`, so they're immune.
- **cmux app quit mid-run** → next call fails; preflight/`BackendUnavailableError`
  gives an actionable message instead of a hang (bounded timeouts).

# Tests Required

Unit (fake `cmux_cli` runner, mirroring the tmux suite):

- `build_workspace` issues `workspace create`, one `new-split` per non-root
  role, a `rename-tab` per pane with the `cs:<inst8>:<role>` title, and
  `send`+`send-key enter` per pane; writes `workspace-state.json`.
- `find_role_pane`/`each_pane` parse `cs:<inst8>:<role>` titles from stub
  `workspace.list`+`surface.list` JSON; wrong-instance and malformed titles are
  skipped.
- `find_workspace(marker)` matches on `current_directory` and returns the
  instance read back from a surface title.
- State getters/`set_run_doc` round-trip through `workspace-state.json`
  (including the `instance=None` → resolve-via-`find_workspace` path).
- `send_role_prompt` single-`send` (no chunking) + submit-confirm retry.
- `check_pane_stall` feeds `capture-pane` text through `stall_decision`.

Unit (`environment.py`): `is_cmux_reachable()` returns False on `Access denied`
and the message names the `socketControlMode` fix; True on `PONG`.

Unit (`config.py`/`get_backend`): `"cmux"` accepted; `get_backend("cmux")`
returns `CmuxBackend`; unknown value still errors.

Integration (gated on a real cmux, `@pytest.mark.skipif` like the tmux headless
suite): the B1/B3 sequence — build 5 panes, tag, send, capture, then rediscover
from a fresh backend instance purely via titles.

# Verification

```
python -m pytest
grep -rn "shell=True" claudespace/backends/cmux_cli.py   # expect: none
```

Manual smoke on a macOS 14+ box with cmux + `socketControlMode=automation`:
`CLAUDESPACE_TERMINAL=cmux claudespace` builds a workspace; a handoff routes.

# Implementation Order

1. **`utils.py`** — cmux bundle-id constant.
2. **`config.py`** — add `"cmux"` to `KNOWN_TERMINAL_BACKENDS` (+ test).
3. **`environment.py`** — `is_cmux_installed`, `is_cmux_reachable` (ping + the
   D4 remediation message), `detect_usable_backends` branch (+ tests).
4. **`cmux_cli.py`** — subprocess wrappers + `CmuxCommandError`; the argv/timeout
   contract. Confirm **Q1 (split targeting)** here against a real cmux and lock
   the chosen path.
5. **`cmux.py`** — `CmuxBackend`, handles, `workspace-state.json` helpers,
   title-based discovery, `run()` preflight (macOS + PATH + reachability),
   `build_workspace`, lookups, send/read/stall, `reveal_role`.
6. **`backends/__init__.py`** — `get_backend` cmux branch.
7. Full `python -m pytest` + the manual smoke.

# Open Questions

- **Q1 — `new-split` source targeting.** Does `cmux new-split <dir>` accept
  `--surface <ref>`, or split only the active surface? → **A (decided):** target
  with `--surface`; if unsupported, `focus` then `new-split`. Both paths
  specified above (D-split); confirm at step 4, no redesign either way.
  *(decided autonomously)*
- **Q2 — is `surface.list` global or workspace-scoped?** → **A (decided):**
  design for workspace-scoped (list workspaces, then surfaces per workspace —
  the proven B3 shape). A global list, if present, is a pure optimization of the
  discovery read. *(decided autonomously)*
- **Q3 — workspace `title`/`custom_title` writability** (spike untested). →
  **A (decided):** not relied upon; identity rides `surface.title` only (D2).
  If a later probe proves it writable, adopting a single workspace-level tag is
  a follow-up optimization, not a correctness fix. *(decided autonomously)*
