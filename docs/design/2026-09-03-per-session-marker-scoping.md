# Per-Session Marker Scoping — Implementation Design

Status: accepted (ready to implement).

## References

- Technical Brief: `docs/research/2026-09-03-per-session-marker-scoping.md`
  (all factual premises below verified there against current source).
- No Planning Brief: this is internal `claudespace` pipeline infrastructure
  with no open product scope. The Technical Brief routed straight here.

This document assumes the reader has the brief open. It covers the *how*; the
*what* and *as-is* live there.

## Problem

Every pipeline marker — the handoff batons and their bookkeeping — lives at a
path built from **repo folder + marker name only**:

```
<repo>/.claudespace/<role>.done  .blocked  .handed-off  .nagged
                   /worktree  /conductor-run  /think  /<role>.stalled
```

Nothing about *which* claudespace session wrote a marker is in its path. The
per-window UUID (`CLAUDESPACE_INSTANCE`) exists, but it only scopes *pane
lookups* — it is not part of any marker filename. Consequences:

- **No same-repo concurrency.** Two pipelines pointed at one repo read and
  write the identical `implementer.done`, `.nagged`, `worktree`, etc. One
  session's baton overwrites or consumes the other's, silently.
- **Sequential leftovers bleed across runs.** A stale sentinel from an earlier
  run is inherited by a later one in the same folder. The motivating bug: a
  leftover `implementer.done.nagged` from item a2 muted the missing-marker nag
  for item a3, so an implementer that forgot its `.done` stopped clean and the
  reviewer was never invoked.

## Goal

Isolate each session's markers so two pipelines on the same repo cannot
collide, and a leftover from one run cannot silence another.

**Non-goal:** changing where committed deliverables live. Research briefs,
designs, review notes follow each project's own doc conventions and never live
in `.claudespace/`; untouched.

## The scope key

`CLAUDESPACE_INSTANCE` — the UUID minted once per `build_workspace`
(`backends/tmux.py:330`, `backends/iterm.py:601`). It is unique per session,
shared by all five roles (stamped on every pane, exported to every pane's
env), and stable across tmux-resurrect restore (baked into the saved launch
command; the session name encodes it). That identity is exactly the unit of
isolation we need.

## Layout

Everything session-owned moves one level deeper, under a per-instance subtree:

```
<repo>/.claudespace/s/<instance>/<role>.done  .blocked  .handed-off  .nagged
                               /worktree  /conductor-run  /think  /<role>.stalled
                               /reports/
```

`s/` is a fixed sub-namespace (`SESSION_DIR = "s"`). Two sessions → two
subtrees → no shared filenames.

---

# Architecture Decisions

The original design decisions D1–D5 stand; restated here with the concrete
signatures the implementer builds against.

### D1 — one layout helper; `instance` threads through `resolve_root` and every `*_marker_path`

A single pure helper in `pipeline.py` defines the layout:

```python
SESSION_DIR = "s"

def session_marker_dir(root: str, instance: str | None) -> str:
    base = f"{root.rstrip('/')}/{MARKER_DIR}"
    return f"{base}/{SESSION_DIR}/{instance}" if instance else base
```

- `instance` present → `<root>/.claudespace/s/<instance>`.
- `instance is None` → `<root>/.claudespace` (flat legacy path).

`session_marker_dir` is a **pure join** — it never calls `resolve_root`. Its
two uses differ in whether the caller passes a resolved or unresolved root:

- The **worktree pointer** is keyed on the *original* launch root (it is the
  thing resolution reads, so it cannot itself depend on resolution):

  ```python
  def worktree_marker_path(root: str, instance: str | None = None) -> str:
      return f"{session_marker_dir(root, instance)}/worktree"

  def resolve_root(root: str, instance: str | None = None) -> str:
      pointer = worktree_marker_path(root, instance)   # unresolved root
      if os.path.isfile(pointer):
          with open(pointer) as f:
              candidate = f.read().strip()
          if candidate and os.path.isdir(candidate):
              return candidate
      return root
  ```

- **Every other builder** resolves first, then scopes:

  ```python
  def done_marker_path(root: str, role: str, instance: str | None = None) -> str:
      return f"{session_marker_dir(resolve_root(root, instance), instance)}/{role}.done"
  ```

  Same shape for `blocked_marker_path`, `conductor_run_marker_path`,
  `think_marker_path`. `instance` is always the **last, optional, keyword-
  defaulted** parameter, so the existing 2-arg call sites and tests keep
  compiling and select the flat path.

No recursion: `instance` is an explicit argument, never re-derived from a
marker. Scoping the worktree pointer per session also lets two runs on one
repo each carve their own worktree without fighting over a shared pointer.

### D2 — `instance=None` is the backward-compat fallback (no migration)

New sessions write scoped. A pipeline **in flight** when this ships has flat
markers and no scoped dir; its panes were launched without
`CLAUDESPACE_MARKER_DIR` / with no scoped `CLAUDESPACE_INSTANCE` reaching the
path builders, so `instance` arrives as `None` and every builder selects the
flat path — that run keeps finding its batons and finishes, then ages out.
Nothing on disk is migrated or moved. Chosen over a hard cutover precisely
because these pipelines run live; a cutover would strand a mid-run baton.

### D3 — out-of-pane callers pass `instance` explicitly

The Stop hook (`handoff.py`) runs *inside* a pane and reads
`CLAUDESPACE_INSTANCE` from env (already does, `main()`). The two callers that
run as the main CLI (no such env) get the instance another way:

- **watchdog** (`watchdog.py`) already receives the workspace's `instance` in
  `_scan_once(..., instance, ...)` and passes it to `backend.each_pane`; the
  stall-marker path takes it from there.
- **`--think` toggle** (`workspace.py`) — see D4.

### D4 — `think` is scoped per session, so the `Window` carries its instance

`--think` is set from the CLI *before* an instance exists, and can also toggle
an already-open workspace. To scope its marker:

- **On build:** write the `think` marker *after* the instance is minted.
  `build_workspace` mints and returns it on the `Window`; `open_workspace`
  writes the marker with `window.instance`.
- **On attach/toggle:** `find_workspace` returns the workspace's
  `@cs_instance` (tmux) / `INSTANCE_VAR` (iTerm2) on the `Window`, so the
  toggle writes into the correct session dir.

This adds an `instance: str` field to the backend window objects and the
`Window` protocol — the only non-mechanical piece the `think` decision costs.

### D5 — `guard.py` needs no change

Its "don't let an agent write into `.claudespace/`" check matches the path
*segment* `/.claudespace/`, which a nested `/.claudespace/s/<instance>/` still
contains.

### Interaction with the `.nagged` bug (must ship together)

Per-session scoping does **not** by itself fix the stale-`.nagged` bug:
consecutive backlog items in one conductor run share a single instance and
therefore a single scope. The nag sentinel must additionally be **run-scoped
by mtime**. In `_maybe_nag_missing_marker`, a `.nagged` file is honored as
"already nagged this run" only if its mtime is at or after the current run's
`run_started`; otherwise it is a leftover from an earlier item — clear it and
nag once (mirrors `_old_run_finished`'s mtime comparison). `run_started` comes
from `backend.get_run_doc(marker=root, instance=instance)`, which already
returns `(current_doc, run_started)`.

- Scoping fixes *cross-session* collisions (two runs, one repo, at once).
- The mtime check fixes *cross-item* leakage (two items, one run, in sequence).

`_maybe_nag_after_handoff_error` (the backend-free fallback) does **not** touch
`.nagged` and needs no change.

---

# Components

- **`pipeline.py`** — `SESSION_DIR`, `session_marker_dir`; `instance` on
  `resolve_root`, `worktree_marker_path`, `done_marker_path`,
  `blocked_marker_path`, `conductor_run_marker_path`, `think_marker_path`.
- **`backends/common.py::launch_command_text`** — export
  `CLAUDESPACE_MARKER_DIR` (below the existing `CLAUDESPACE_*` exports).
- **`backends/base.py::Window`** — add `instance: str` to the protocol;
  `build_workspace`/`find_workspace` return it.
- **`backends/tmux.py`** — `instance` field on `TmuxWindow`; populate in
  `build_workspace` (already in scope) and in `find_workspace` (from the
  `@cs_instance` row `_matching_rows` already reads, tmux.py:470).
- **`backends/iterm.py`** — instance on the iTerm2 window handle; populate in
  `build_workspace` and in `_find_workspace_window` (read `INSTANCE_VAR` off
  the matched session).
- **`handoff.py`** — thread `instance` (already read in `main()`) into the
  marker-path builders; the `.nagged` mtime fix in `_maybe_nag_missing_marker`.
- **`watchdog.py`** — `instance` into `_stall_marker_path` (via
  `session_marker_dir` + `resolve_root`).
- **`workspace.py`** — instance-aware `_set_think` on both build and attach;
  move the write past instance discovery.
- **`assets/prompts/*.prompt.md`** (6 files, ~67 refs) —
  `$CLAUDESPACE_ROOT/.claudespace/…` → `$CLAUDESPACE_MARKER_DIR/…`, plus the
  Worktree-section recompute line.

# Delivery: one env var

The scope reaches panes through a single new export in `launch_command_text`,
next to the existing `CLAUDESPACE_ROOT` / `_ROLE` / `_INSTANCE` exports:

```python
effective_root = resolve_root(root, instance)
marker_dir = session_marker_dir(effective_root, instance)
# ... export CLAUDESPACE_ROOT={effective_root} && ...
# ... export CLAUDESPACE_MARKER_DIR={marker_dir} && ...
```

- **Prompts** write to `$CLAUDESPACE_MARKER_DIR/…` instead of
  `$CLAUDESPACE_ROOT/.claudespace/…`.
- **Python** builds the identical path from `(root, instance)`.

Both sides land on the same directory without either hard-coding the other's
convention. `launch_command_text` already passes `root` as the *original*
launch root, so `resolve_root(root, instance)` follows a worktree pointer
recorded since — same as every other builder.

# Data Flow

Marker lifecycle for one handoff, scoped session `I` on repo `R` (no worktree):

1. Pane launches. `launch_command_text` exports
   `CLAUDESPACE_ROOT=R`, `CLAUDESPACE_INSTANCE=I`,
   `CLAUDESPACE_MARKER_DIR=R/.claudespace/s/I`.
2. Role finishes, writes `$CLAUDESPACE_MARKER_DIR/<role>.done` (prompt-driven;
   `mkdir -p $CLAUDESPACE_MARKER_DIR` first).
3. Stop hook fires → `handoff.main()` reads `role`, `root=R`, `instance=I`
   from env → `_run` → `_send_handoff`.
4. `done_marker_path(R, role, I)` = `R/.claudespace/s/I/<role>.done`; content
   read, `<role>.done.handed-off` written *in the same scoped dir*, next
   role's pane prefilled.
5. If no marker: `_maybe_nag_missing_marker` fetches `run_started` via
   `get_run_doc`, honors `<role>.done.nagged` only if fresh for this run, else
   clears + nags once.

Worktree variant: the role that creates the worktree writes the pointer to
`R/.claudespace/s/I/worktree` (its `CLAUDESPACE_MARKER_DIR` still points at the
original root at that moment), then re-exports `CLAUDESPACE_ROOT=<worktree>`
and recomputes `CLAUDESPACE_MARKER_DIR=<worktree>/.claudespace/s/I`. Every
later pane's `launch_command_text` calls `resolve_root(R, I)`, reads the
pointer at the original-root session dir, and lands its
`CLAUDESPACE_MARKER_DIR` inside the worktree — pointer stays at the original
root, all other markers live in the worktree (exactly today's behavior, now
instance-scoped).

# Prompt changes (the ~67 refs)

Two mechanical substitutions per file, plus the Worktree recompute:

1. Every `$CLAUDESPACE_ROOT/.claudespace/<name>` → `$CLAUDESPACE_MARKER_DIR/<name>`
   (done/blocked markers, `reports/`, `think`, `conductor-run`, the
   `implementer.blocked` read, etc.). Every `mkdir -p
   $CLAUDESPACE_ROOT/.claudespace` → `mkdir -p $CLAUDESPACE_MARKER_DIR`.
2. **Worktree section**, two spots:
   - The *read*: `$CLAUDESPACE_ROOT/.claudespace/worktree` →
     `$CLAUDESPACE_MARKER_DIR/worktree` (defensive; `launch_command_text` has
     already resolved and `cd`'d, so this is belt-and-suspenders as today).
   - The *write + re-export*: after `export CLAUDESPACE_ROOT=<that path>`, add
     `export CLAUDESPACE_MARKER_DIR="$CLAUDESPACE_ROOT/.claudespace/s/$CLAUDESPACE_INSTANCE"`
     so the role's own remaining writes this turn land where downstream panes
     will look. The pointer write itself stays
     `$CLAUDESPACE_MARKER_DIR/worktree` (still the original-root session dir at
     that moment).

Edit only `claudespace/assets/prompts/*.prompt.md`. **Do not** edit
`build/lib/claudespace/assets/prompts/*` — build artifacts.

# Validation

- `session_marker_dir(root, None)` must be byte-identical to today's flat path
  (`<root>/.claudespace`) — this is the D2 fallback contract; assert it.
- `instance` is opaque; no validation beyond truthiness (`None`/`""` → flat).
  The UUID is minted internally, never user-supplied.

# Error Handling

- Path builders are pure string ops and never raise on a `None` instance.
- The `.nagged` mtime read is inside `_run`'s existing try in `handoff.main()`;
  a `get_run_doc` failure there is already caught and surfaced via
  `_maybe_nag_after_handoff_error`. When `run_started` is `None` (no run doc
  yet), treat any existing `.nagged` as valid (do not re-nag) — matches
  `_old_run_finished`'s `run_started is None → True` guard, avoiding a spurious
  nag before a run is recorded.

# Security Considerations

None new. `guard.py`'s `/.claudespace/` segment match still fires on the
deeper subtree (D5). No new user-controlled path components — `instance` is an
internally minted UUID; `s` is a constant.

# Performance Considerations

Negligible. One extra `s/<instance>` path segment and one extra
`os.path.isfile` per marker read (already the pattern). The `.nagged` fix adds
one `get_run_doc` call inside the nag branch only — which runs at most once per
missing-marker streak, not per Stop. No new collection scans, no N+1.

# Compatibility

- **Backward compatible by construction** (D2): `instance` defaults to `None`
  everywhere → flat paths, so in-flight flat runs and pre-export panes keep
  working. No migration, no on-disk move.
- **Deprecation:** the flat path is not removed; it remains the `instance is
  None` branch indefinitely as the compat floor. No follow-up cleanup ships.
- Existing 2-arg `*_marker_path` / `resolve_root` call sites and their tests
  are unchanged — the new parameter is trailing and optional.

# Edge Cases

- **Old pane, no `CLAUDESPACE_INSTANCE`:** `instance=None` → flat path; run
  finishes on legacy markers. (D2.)
- **Worktree pointer under a scoped dir the reader can't see:** covered — the
  pointer is always keyed on the *original* launch root's session dir, which
  `resolve_root(root, instance)` reads with the unresolved root. A pane already
  launched into the worktree finds nothing at
  `$CLAUDESPACE_MARKER_DIR/worktree` (its dir resolved into the worktree) but
  is already `cd`'d there, so the read is a harmless no-op — same as today.
- **Two runs, same repo, concurrent:** distinct instances → distinct subtrees
  → no shared filenames. Isolated, not serialized (see "What this does not
  solve").
- **Stale `.nagged` from a prior backlog item, same run:** mtime < current
  `run_started` → cleared and re-nagged once. (The motivating bug.)
- **`--think` toggle on an attached workspace:** `find_workspace` supplies the
  live instance; the toggle writes/removes the scoped `think` marker so a
  mid-run flip reaches roles that re-read it at handoff.

# Tests Required

Unit (`tests/test_pipeline.py`):
- `session_marker_dir(root, None)` == flat path; with instance == `s/<id>`.
- Each `*_marker_path(root, role, "id")` lands under `s/id/`; the 2-arg form
  still lands flat (extend the existing trailing-slash test).
- `resolve_root(root, "id")` reads the pointer at `s/id/worktree`; the 1-arg
  form still reads the flat `worktree` (existing tests keep passing unchanged).

Unit (new `tests/test_handoff.py`, or extend an existing module):
- `_maybe_nag_missing_marker`: `.nagged` newer than `run_started` → no re-nag;
  older → cleared and nags once; `run_started is None` → treated as valid.

Unit (`tests/test_tmux_backend.py`, `tests/test_iterm.py`):
- `find_workspace` returns a window whose `.instance` matches the built
  session's `@cs_instance` / `INSTANCE_VAR`.
- `launch_command_text` output contains
  `export CLAUDESPACE_MARKER_DIR=<root>/.claudespace/s/<instance>` (and the
  worktree-resolved variant when a pointer exists).

Integration:
- Two `session_marker_dir` subtrees for two instances on one root do not share
  a `<role>.done` path.

No end-to-end (terminal-driving) test is added; both backends are covered by
their existing unit suites plus the `launch_command_text` assertion.

# Verification

```
python -m pytest
```

Plus a manual smoke on the prompt substitution:

```
grep -rn 'CLAUDESPACE_ROOT/\.claudespace' claudespace/assets/prompts/   # expect: none
grep -rn 'CLAUDESPACE_MARKER_DIR' claudespace/assets/prompts/           # expect: every former ref
```

# Implementation Order

1. **`pipeline.py`** — add `SESSION_DIR`, `session_marker_dir`; add trailing
   optional `instance` to `resolve_root`, `worktree_marker_path`, and all four
   `*_marker_path` builders, routing through `session_marker_dir`. Land its
   unit tests. (Self-contained; nothing else depends on the new param yet.)
2. **`backends/common.py`** — compute and export `CLAUDESPACE_MARKER_DIR` in
   `launch_command_text` from `(root, instance)`. Add its assertion test.
3. **`backends/base.py` + `tmux.py` + `iterm.py`** — add `instance` to the
   `Window` protocol and both window objects; populate in `build_workspace`
   and `find_workspace` (tmux from `@cs_instance` rows; iTerm2 from
   `INSTANCE_VAR`). Add the `find_workspace().instance` tests.
4. **`workspace.py`** — move `_set_think` past instance discovery: call
   `_set_think(resolved_root, existing.instance, think)` on attach and
   `_set_think(resolved_root, window.instance, think)` after build; give
   `_set_think` the `instance` param and `makedirs` the session dir.
5. **`handoff.py`** — pass `instance` into every marker-path builder; implement
   the `.nagged` mtime fix in `_maybe_nag_missing_marker` (fetch `run_started`
   via `get_run_doc`). Add the nag tests.
6. **`watchdog.py`** — thread `instance` into `_stall_marker_path` via
   `session_marker_dir(resolve_root(root, instance), instance)`.
7. **`assets/prompts/*.prompt.md`** (6 files) — the two substitutions + the
   Worktree recompute line. Run the two `grep` checks.
8. Full `python -m pytest`.

# Compatibility / rollout

Ships as one change; no feature flag. In-flight flat runs are handled by the
D2 `instance=None` fallback, so no coordination with running pipelines is
needed.

# What this does not solve

- Two sessions still share the repo's git working tree unless each uses its own
  worktree; scoping isolates *markers*, not the checkout.
- No cross-session coordination (locks, queueing). Concurrent runs are
  isolated, not serialized.

# Open Questions

None. Every decision is settled above; the Technical Brief confirmed all
factual premises.
