# Ghostty Terminal Support (via a tmux backend) — Implementation Design

## References

- Planning Brief: `docs/planning/2026-09-02-ghostty-terminal-support.md` (tmux-backed revision)
- Technical Brief: `docs/research/2026-09-02-ghostty-terminal-support.md`

The "what" (scope, acceptance criteria) and the "as-is" (iTerm2 coupling, affected surfaces) live in those documents. This covers only the "how".

**Design revision note.** An earlier draft of this design targeted a native `GhosttyBackend` on Ghostty's 1.3 AppleScript API. That path was bounced to planner because Ghostty's API has no screen-read and no per-pane-variable primitives, forcing best-effort prompt confirmation, a crash-detection-only watchdog, and an ad-hoc file state-store. Planner re-scoped to **tmux-everywhere** (Planning Brief Assumptions). tmux's `capture-pane` and pane-scoped user options (`set -p @…`) supply exactly those two missing primitives, so this design delivers *full* parity. The abstraction from the earlier draft (interface, config selection, consumer routing) is unchanged; only the second backend changes — a `TmuxBackend`, not a `GhosttyBackend`.

tmux facts this design commits to (all long-standard, tmux ≥ 3.0):

- Pane content: `tmux capture-pane -p -t <pane>` prints the visible screen to stdout → readiness/submit-confirm/watchdog content-diff.
- Per-pane state: `tmux set-option -p -t <pane> @k v` / `show-options -p -v -t <pane> @k` → user-variable equivalent.
- Input: `tmux send-keys -t <pane> -l -- <text>` (literal) then `send-keys -t <pane> Enter`.
- Structure: `new-session -d` (detached server, no terminal needed), `split-window`, `new-window`, `select-pane -t`, `select-window`, `list-panes -a -F <fmt>`, `kill-session`.
- Stable ids: `#{pane_id}` (`%N`) is stable for a pane's life; pane dims via `#{pane_width}`/`#{pane_height}` (so split-sizing has full fidelity, unlike raw Ghostty).

---

## Architecture Decisions

### AD1 — Introduce a `TerminalBackend` interface; keep the iTerm2 backend as a verbatim wrap of today's `iterm.py`

Define a `TerminalBackend` abstract interface whose method surface is exactly today's `claudespace.iterm` public surface (`build_workspace`, `reveal_role`, `find_workspace`, `find_role_pane`, `each_pane`, `activate_window`, `activate_pane`, `send_role_prompt`, `send_new`, `get_auto_handoff`, `get_lazy`, `get_template_name`, `get_run_doc`, `set_run_doc`, `close_window_if_empty`). `ItermBackend` implements it by delegating to the existing functions, moved under `claudespace/backends/iterm.py` essentially unchanged. `TmuxBackend` (`claudespace/backends/tmux.py`) reimplements the same surface via the `tmux` CLI. A factory in `claudespace/backends/__init__.py` selects one from config; consumers call `backend.<method>` instead of `claudespace.iterm.<function>`.

**Reasoning.** The dominant constraint (Planning Brief Constraints; AC8; FR9) is *zero regression to the existing iTerm2 path*. Wrapping the current, proven module verbatim keeps its behavior byte-for-byte identical — a move + a delegating class, not a rewrite. The risk budget goes to the new backend.

**Rejected alternative — thin-primitive interface with shared orchestration.** Expose low-level primitives and lift `build_workspace`/`reveal_role`/`send_role_prompt` into shared functions. Cleaner in theory, but forces decomposing the iTerm2 module — the one thing the hard constraint says must not move. Drift risk is instead mitigated by AD2 and by the interface being small (15 methods).

### AD2 — Backend-independent helpers live in a shared module

`role_prompt_file`, `role_prompt_prefix`, `_command_with_baked_persona` (`--append-system-prompt-file`/`--name`), the per-pane env/`cd` launch-command string, and the timing constants (`CLAUDE_READY_TIMEOUT_SECONDS`, `SUBMIT_KEYSTROKE_SETTLE_SECONDS`, etc.) are pure functions of role/config and know nothing about the terminal. They move to `claudespace/backends/common.py`, called by both backends, so persona-baking and prompt semantics can't drift.

### AD3 — tmux is a detached server; the terminal is a dumb viewport

The workspace is a **detached tmux session** (`tmux new-session -d -s <session>`), built and driven entirely through the `tmux` CLI — pane creation, prompt injection, state, watchdog, handoff all operate on the tmux server whether or not any terminal is attached. To make it visible, claudespace spawns the user's terminal running `tmux attach -t <session>` (default Ghostty for this backend; see AD5).

**Reasoning.** This is the key structural decision and it directly answers Planning Brief Risk 3 (residual dependency on Ghostty's preview automation). Because the server is detached, building a workspace needs the terminal only to be **launchable as a process**, never *scriptable* — no AppleScript, no automation-API dependency on Ghostty at all. The Stop-hook handoff flow and the watchdog run `tmux` commands against the server regardless of attachment, so they work even if the user detached or closed the viewport. It also makes the whole backend testable headlessly (no terminal, no display) — a large win for CI and the test suite.

**Session/window model.** One claudespace workspace = one tmux session named from the workspace marker+instance (`cs-<sha1(marker)[:8]>-<instance[:8]>`). Eager mode builds panes via `split-window`/`select-layout` inside the session's window; lazy mode starts one pane and `split-window`s on reveal. tmux windows-vs-panes map to today's tab-vs-pane split the same way the iTerm2 layouts already assume.

### AD4 — Per-pane state via tmux user options (no file store)

iTerm2 stores workspace state as **session user-variables** (`WORKSPACE_VAR`, `INSTANCE_VAR`, `ROLE_VAR`, `AUTO_HANDOFF_VAR`, `LAZY_VAR`, `TEMPLATE_VAR`, `RUN_DOC_VAR`, `RUN_STARTED_VAR`). tmux pane-scoped user options are the direct equivalent: each pane gets `@cs_workspace`, `@cs_instance`, `@cs_role`, `@cs_auto_handoff`, `@cs_lazy`, `@cs_template`, `@cs_run_doc`, `@cs_run_started`, set with `set-option -p` and read with `show-options -p -v` (or bulk via `list-panes -a -F`).

**Reasoning.** This is a 1:1 mechanical mapping of the iTerm2 identity model — no ad-hoc JSON file-store, no flock/atomic-write, no liveness cross-check (the earlier native-Ghostty draft's AD3 disappears entirely). State lives on the live pane exactly as iTerm2's does; a closed pane takes its state with it, matching iTerm2 semantics for free. `_matches_workspace`'s `(marker, instance)` invariant is preserved: `find_role_pane`/`each_pane` enumerate `tmux list-panes -a -F '#{pane_id}|#{@cs_workspace}|#{@cs_instance}|#{@cs_role}'` and filter, replacing iTerm2's window/tab/session walk.

### AD5 — Backend selection via new `~/.config/claudespace/config.toml`; viewer terminal configurable

No general config file exists today — only `templates.toml`. Add `~/.config/claudespace/config.toml`:

```toml
[terminal]
backend = "tmux"          # or "iterm2"; absent ⇒ "iterm2"

[terminal.tmux]
viewer = "ghostty"        # which terminal hosts `tmux attach`; default "ghostty"
session_prefix = "cs"     # optional
```

`CLAUDESPACE_TERMINAL=tmux|iterm2` env var overrides the file (for tests/one-offs; the Planning Brief forbids a *per-command flag* as the primary UX — an env override for testing is not that). iTerm2 is default when neither is set (AC8, FR1). `viewer` decouples "run via tmux" from "which terminal window shows it," so the same backend serves the Ghostty goal without being hard-wired to Ghostty (Planning Brief Assumptions: tmux is the mechanism, Ghostty the goal).

### AD6 — Watchdog keeps full-fidelity content-stall detection via `capture-pane`

`watchdog.py` detects stalls by screen-content diffing (`_screen_signature` line 47; `_check_once` calls `async_get_screen_contents` line 100). `tmux capture-pane -p -t <pane>` returns the same visible text, so the existing diff logic is preserved wholesale — a pane byte-identical across an interval and not showing the `❯` ready marker is flagged, exactly as on iTerm2 (FR7/AC6, full fidelity — the native-Ghostty draft's crash-detection-only descope is gone). Only the content *source* is abstracted behind a backend method; the signature/marker/notify machinery stays shared.

### AD7 — Correct home: this package, new `claudespace/backends/` subpackage

Single-package repo, no shared/upstream package (Technical Brief). New `backends/` subpackage: `__init__.py` (factory), `base.py` (interface + `Pane`/`Window` handle protocols), `iterm.py` (moved), `tmux.py`, `common.py`, `tmux_cli.py` (thin async `tmux` subprocess wrapper).

---

## Components

New (`claudespace/backends/`):

- `base.py` — `TerminalBackend` ABC; `Pane`/`Window` typing protocols (opaque handles; iTerm2's wrap `iterm2.Session`/`Window`, tmux's wrap a `pane_id`/session-name dataclass).
- `iterm.py` — today's `claudespace/iterm.py` moved here; `ItermBackend` delegates each method to the existing function; `connect.py`'s connection/retry becomes `ItermBackend.run`.
- `tmux.py` — `TmuxBackend`; orchestration mirrors `common.py`'s shared build/reveal/send logic, using `tmux_cli` primitives.
- `tmux_cli.py` — async wrappers over `tmux` subcommands (argv, never shell strings): `new_session`, `split_window`, `new_window`, `send_keys`, `capture_pane`, `set_pane_opt`, `show_pane_opt`, `list_panes`, `select_pane`, `select_window`, `kill_session`, `has_session`, `pane_dims`.
- `common.py` — AD2 shared helpers + timing constants.
- `__init__.py` — `get_backend()` factory (AD5).

Modified consumers (per Technical Brief "Affected Surfaces" + the consumer-map investigation; each stops importing `iterm`/`iterm2` and uses a `TerminalBackend`):

- `cli.py` — resolve backend at entry; replace `connect.run(coro)` (lines 236, 274) with `backend.run(entrypoint)`.
- `connect.py` — iTerm2 connection logic folds into `ItermBackend.run`; module becomes iTerm2-internal or retired. **Two further direct connection entry points** exist outside `cli.py`: `handoff.py:620` and `messaging.py:170` each call `iterm2.run_until_complete(...)` (the Stop-hook and `claudespace-msg` flows). Both must route through `backend.run` (for `TmuxBackend`, `run` simply drives the coroutine on an asyncio loop — no persistent connection).
- `config.py` — add `load_terminal_backend()` reading `config.toml`/env; template logic untouched.
- `workspace.py` — attach-or-build via `backend.find_workspace`/`build_workspace`/`close_window_if_empty`/`activate_window`; cold-launch/`just_launched_iterm` handling gets a tmux/viewer branch (spawn viewer terminal attaching to the session).
- `handoff.py` — largest consumer; all `iterm_ops.*` session/window calls (get_run_doc, set_run_doc, find_role_session, send_new, get_template_name, get_lazy, reveal_role, get_auto_handoff, role_prompt_prefix, send_role_prompt, activate_session) route through the backend; `iterm2.async_get_app` usages (272, 407, 578) fold into backend handles.
- `watchdog.py` — `async_get_screen_contents` (line 100) → `backend.capture_pane`; `each_pane`, `ROLE_VAR` read, stall-marker/notify machinery stay/route through backend (AD6). Matches by root marker alone (`instance=None`), unchanged.
- `messaging.py` — `claudespace-msg` resolves the role pane via `backend.find_role_pane` and sends via `backend.send_role_prompt`.
- `layouts.py` — `Layout.build(root_session)` becomes backend-aware: `SplitNode.build` (line 83) uses `async_split_pane` (iTerm2) or `tmux split-window` (tmux). Layout *tree* stays shared; `build` takes the backend / a split callback.
- `themes.py` — `build_role_profile` (`iterm2.Color`, `LocalWriteOnlyProfile`) is iTerm2-only; tmux applies role identity via pane-border style/title (`select-pane -T`, `pane-border-format`) and `claude --name`. `banner_command` stays shared.
- `utils.py` — `is_iterm_running`/`launch_iterm` are process-level; add `is_tmux_available` (PATH/`tmux -V`) and viewer-launch (`launch_viewer`) peers, selected by backend.
- `environment.py` — iterm2.com help text + API-socket check are iTerm2-specific; relocate behind `ItermBackend`, keep shared macOS checks.

---

## Data Flow

```
CLI command (cli.py)
    ↓  get_backend()  →  ItermBackend | TmuxBackend
backend.run(entrypoint)
    ↓
workspace.py / handoff.py        # role dispatch, reveal — unchanged logic
    ↓  backend.<method>(...)
ItermBackend → iterm2 RPC        |  TmuxBackend → `tmux` CLI (detached server)
                                                  ↑ viewer terminal (Ghostty) attaches
```

Ghostty-hosted handoff lifecycle (representative): a role's Stop hook fires → `handoff.py` reads flags/run-doc/template via `backend.get_*` (tmux: `show-options -p`) → resolves `backend.find_role_pane(role)` (tmux: `list-panes -a -F` filtered by `@cs_*`) → if absent and lazy, `backend.reveal_role` (tmux: `split-window` off the largest pane by `#{pane_width}×#{pane_height}`, capture new `#{pane_id}`, `set-option -p @cs_role`, launch via `send-keys`) → `backend.activate_pane` (`select-pane`/`select-window`) → `backend.send_role_prompt` (`send-keys -l`, settle, `send-keys Enter`, `capture-pane` confirm-and-retry). All against the tmux server; the viewer need not even be focused.

---

## API Changes

None external. New internal `TerminalBackend` interface (AD1) and config surface `config.toml`/`CLAUDESPACE_TERMINAL` (AD5).

## Database Changes

None. No file state-store (AD4 uses tmux user options). No migration.

---

## Validation

- `get_backend()` validates the value against `{"iterm2", "tmux"}`; unknown ⇒ fast named startup error (mirrors `get_template`'s style), never silent fallback.
- tmux selected but `tmux` absent from PATH, or version below the `set -p`/`capture-pane` floor → clear error (Error Handling).
- `viewer` terminal not launchable → clear error, but the detached session is left intact so the user can `tmux attach` manually.
- Non-macOS with tmux selected → same clear error path (macOS-only scope).

## Error Handling

FR8/AC7 — clear, actionable failure; never hang, never silent fallback.

- `TmuxBackend.run` preflights: `tmux -V` present and new enough; else "tmux is required for the tmux backend and was not found on PATH — install it (brew install tmux) or set terminal.backend = \"iterm2\"."
- Viewer launch failure (Ghostty not installed / not launchable) → name the viewer and how to change it (`[terminal.tmux] viewer`), and note the session is running detached (`tmux attach -t <name>` to reach it).
- Every `tmux_cli` call checks exit status and surfaces stderr; a nonzero/timeout raises `BackendUnavailableError`, never an indefinite wait (the failure mode `connect.py` exists to prevent, reproduced here).
- No cross-backend fallback: a failed tmux selection exits with the message, never quietly builds in iTerm2 (AC7 explicit).

## Security Considerations

- `tmux_cli` builds argv arrays (`create_subprocess_exec`), never shell strings — no command injection through a crafted path, template command, or role name reaching `send-keys`/`set-option`. Note `send-keys -l -- <text>` and `--` guards to keep prompt text from being parsed as options.
- No new network surface. tmux socket is per-user under `$TMPDIR` with tmux's own perms, matching the trust boundary of the iTerm2 API socket today.

## Performance Considerations

- iTerm2 path: unchanged.
- Each tmux primitive is one short-lived `tmux` subprocess (~single-digit ms, local socket). Hot paths:
  - `find_role_pane`/`get_*`: one `list-panes -a -F` or `show-options -p` — O(panes), bounded (≤6), no N+1.
  - `each_pane` (watchdog, `set_run_doc`): a single `list-panes -a -F` returns every pane + its `@cs_*` options in one call — cheaper than iTerm2's per-session variable fetches.
  - Readiness/submit/watchdog polling: one `capture-pane` per tick, time-bounded by the existing constants.
- No file-store contention (AD4). tmux serializes server commands internally, so concurrent Stop-hook handoffs are safe without app-level locking.

## Compatibility

- **Backward.** iTerm2 remains default and untouched (AD1); existing workspaces/behavior unaffected (FR9/AC8). Absent `config.toml` ⇒ iTerm2. No migration.
- **New dependency.** tmux (≥ 3.0 for pane user options) required only when the tmux backend is selected; surfaced as an actionable error otherwise (Planning Brief Constraints/Risks).
- **Viewer.** Default Ghostty; any launchable terminal works via `viewer`. Because the server is detached, a viewer/terminal upgrade or breakage never corrupts a running workspace.

---

## Edge Cases

- **Two windows, same root** (stale + `--new`, or two worktrees to one real path): distinct tmux sessions keyed by `instance`; `@cs_instance` filtering preserves `_matches_workspace` semantics.
- **User already inside tmux** when launching: claudespace targets its own **named** session on the default socket and never issues bare (client-relative) commands, so a nested/attached client can't cross-wire. `send-keys -t <session>:<pane_id>` is always fully qualified.
- **User's tmux config/plugins interfering** (Planning Risk): build the claudespace session with an explicit, minimal server env where it matters (fully-qualified target ids; not relying on user key-tables for automation, which uses `send-keys` not key bindings). Prefix-key/status-bar cosmetics are the user's; automation doesn't depend on them.
- **Viewer closed / detached mid-run**: session and pipeline continue on the detached server; handoffs and watchdog keep working. Re-attach with `tmux attach`. (Strictly better than iTerm2, where closing the window ends the workspace.)
- **Readiness & submission**: `capture-pane` gives real screen text, so `_wait_for_claude_prompt` (`❯` marker) and `_confirm_submitted` (probe text cleared from input) port directly — full confirmation, not best-effort (FR3/AC2).
- **Lazy reveal split sizing**: tmux reports `#{pane_width}`/`#{pane_height}`, so `_largest_sibling` picks the biggest pane exactly as iTerm2 does — no sliver degradation.
- **Theming**: iTerm2 profile colors/badge → tmux pane-border style + title; `--name`/banner still convey role identity. Cosmetic-only difference.
- **`--think`/max-items** and other flags flow through unchanged (env/marker driven).

---

## Tests Required

Unit:
- `get_backend()` selection matrix: unset⇒iterm2, `config.toml` value, env precedence, invalid⇒named error.
- `common.py`: `role_prompt_prefix`/`_command_with_baked_persona` unchanged by the move (existing tests pass at the new location).
- `tmux_cli.py`: each wrapper builds the expected argv; nonzero/timeout ⇒ `BackendUnavailableError` with the right message class; `-l --` guards present.
- `TmuxBackend` with a faked `tmux_cli` runner: build/reveal/find/each/send map to the expected command sequences; state round-trips through `@cs_*`; readiness reads `capture-pane`; submit confirm-and-retry loop drives on stale capture then clears; largest-sibling uses pane dims; preflight failures ⇒ correct errors.

Integration:
- `ItermBackend` delegation: existing `iterm.py` tests re-pointed at the backend surface prove no behavior change (regression guard for Constraint #1).
- `handoff.py`/`workspace.py`/`watchdog.py`/`messaging.py` against a fake `TerminalBackend`: no `iterm2` touches; identical drive for both backends.
- **Headless tmux integration** (real `tmux -f /dev/null`, detached, no terminal): build a workspace, set/read `@cs_*`, split on reveal, `send-keys`+`capture-pane` round-trip, `each_pane` enumeration, watchdog content-diff on a scripted stalled pane, `kill-session`. This is the payoff of AD3 — the tmux backend is fully testable without a display.

End-to-end (macOS + Ghostty + tmux, gated/manual): full pipeline in a Ghostty-hosted tmux session covering AC1–AC7 (build, confirmed prompt inject, handoff reveal, `claudespace-msg`, full-fidelity watchdog, tmux-missing error, viewer-launch error).

## Verification

- `uv run pytest` — full suite green, incl. re-pointed iTerm2 delegation tests and the headless tmux integration tests.
- `uv run ruff check` / formatter per repo standard.
- Manual macOS smoke: `CLAUDESPACE_TERMINAL=iterm2` full run (regression); `CLAUDESPACE_TERMINAL=tmux` full run in Ghostty (AC1–AC6); tmux-uninstalled and viewer-missing runs (AC7 messages).

---

## Implementation Order

1. **Extract shared helpers** (`backends/common.py`): move `role_prompt_file`, `role_prompt_prefix`, `_command_with_baked_persona`, launch-command builder, timing constants out of `iterm.py`; re-point tests. No behavior change.
2. **Define the interface** (`backends/base.py`): `TerminalBackend` ABC + `Pane`/`Window` protocols, matching today's `iterm.py` public surface (AD1).
3. **Move iTerm2 into a backend** (`backends/iterm.py` + `ItermBackend`): relocate verbatim behind delegating methods; fold `connect.py` into `ItermBackend.run`. Re-pointed existing tests must pass unchanged — the regression gate.
4. **Backend selection** (`config.load_terminal_backend`, `backends/__init__.get_backend`, AD5) + `config.toml`/`CLAUDESPACE_TERMINAL`; wire `cli.py` to `backend.run`. Default resolves to `ItermBackend`.
5. **Route consumers through the backend**: `workspace.py`, `handoff.py` (incl. :620), `watchdog.py` (screen → `capture_pane` method), `messaging.py` (incl. :170), `layouts.py` (split callback), `themes.py`, `utils.py`. Full suite green on iTerm2 after this step; tmux not yet built — the abstraction is proven with zero regression before any tmux code exists.
6. **tmux CLI layer** (`backends/tmux_cli.py`, AD3): async argv wrappers + error/timeout handling + preflight (`tmux -V`, version floor).
7. **`TmuxBackend`** (`backends/tmux.py`): detached-session build/reveal, `@cs_*` state (AD4), `send-keys`/`capture-pane` prompt inject + confirm, largest-sibling via pane dims, watchdog `capture_pane` (AD6), viewer launch (`utils.launch_viewer`), `run` preflight + error classes (AC7).
8. **Theming/viewer polish** (`themes.py` tmux branch: pane-border role labels) and docs note that claudespace-in-Ghostty runs via tmux (Planning Risk mitigation).
9. **Tests** per above — unit, iTerm2 delegation regression, fake-backend consumer tests, headless tmux integration — then macOS manual E2E.

Steps 1–5 land the abstraction with the iTerm2 path provably unchanged (the hard constraint); 6–8 add the tmux backend; each step keeps the suite green.

---

## Open Questions

- **Viewer launch mechanism per terminal.** The one remaining terminal-specific detail is *how* to spawn the viewer attaching to the detached session — `ghostty -e tmux attach -t <name>` vs. `open -na Ghostty --args …` vs. a terminal-specific incantation. Low risk (it's a process spawn, and failure leaves the session reachable via manual `tmux attach` — Error Handling covers it), but confirm the exact Ghostty invocation in step 7; keep `launch_viewer` a small per-viewer lookup so other viewers are a one-line addition.
- No open *product* questions — the Planning Brief's decisions (tmux-everywhere, opt-in/experimental, full parity, iTerm2 default, macOS-only, Ghostty-as-goal-not-restriction) are taken as given.
