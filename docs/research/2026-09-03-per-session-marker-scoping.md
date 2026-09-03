# Technical Brief — Per-Session Marker Scoping

## Original Request

Forwarded doc: `docs/design/2026-09-03-per-session-marker-scoping.md`, a
"Status: proposed (not yet implemented)" design for scoping `.claudespace/`
pipeline markers per session (`CLAUDESPACE_INSTANCE`) instead of per repo, to
fix same-repo concurrency collisions and stale-marker leakage across runs.

## Summary

Verified the design doc's factual premises against the current codebase: all
of them hold. Markers are built from `root` + fixed marker name only
(`claudespace/pipeline.py`), `CLAUDESPACE_INSTANCE` exists and is minted/
exported per session but is not part of any marker path, `Window`/
`TmuxWindow` carry no `instance` field back to callers, and `.nagged` is a
pure presence check with no run/mtime scoping. This is a design document, not
a change request with open product scope — no new investigation was needed
beyond confirming the doc's claims are accurate.

## Current Behaviour

- `claudespace/pipeline.py`: `MARKER_DIR = ".claudespace"`. Every
  `*_marker_path` builder (`done_marker_path`, `blocked_marker_path`,
  `conductor_run_marker_path`, `think_marker_path`, `worktree_marker_path`)
  concatenates `resolve_root(root)` + `MARKER_DIR` + a fixed filename — no
  instance/session component anywhere.
- `resolve_root(root)` (pipeline.py:212) only follows the `worktree` marker
  (if present and pointing at a real dir); it takes no `instance` parameter
  today.
- `claudespace/backends/common.py::launch_command_text` (line 108) exports
  `CLAUDESPACE_ROOT`, `CLAUDESPACE_ROLE`, `CLAUDESPACE_INSTANCE`,
  `CLAUDESPACE_MAX_ITEMS`, `CLAUDESPACE_THINK`, `CLAUDESPACE_TERMINAL` into
  every pane's launch command. No `CLAUDESPACE_MARKER_DIR` export exists.
- `CLAUDESPACE_INSTANCE` is minted as `str(uuid.uuid4())` once per
  `build_workspace` in both backends (`tmux.py:330`, `iterm.py:601`), stamped
  on every pane (tmux: `@cs_instance` custom option, tmux.py:122,257;
  iTerm2: `INSTANCE_VAR` session variable, iterm.py:317) and used today only
  to scope *pane lookups* — `_matching_rows`/`_matches_workspace` filter
  panes by `instance` when finding/messaging a workspace's own panes
  (tmux.py:227-234, iterm.py:392-414). It is never read into a marker path.
- `TmuxWindow` (tmux.py:144) and the `Window` protocol (`base.py:30`) carry
  only `session` — no `instance` field. `find_workspace` (tmux.py:443,
  iterm.py:655) returns a bare `Window`/`TmuxWindow`, so a caller with only
  the returned window object cannot recover the session's instance today —
  confirms the design's D4 claim that this must be added.
- `claudespace/handoff.py`: `.nagged` state is a pure sentinel file
  (`_already_nagged` at line 98 checks `os.path.isfile(done_path +
  ".nagged")`; `_mark_nagged`/`_clear_nag` create/remove it). No mtime or
  `run_started` comparison exists — confirms the doc's "Interaction with the
  `.nagged` bug" section: a leftover `.nagged` from an earlier backlog item
  in the same run/instance is indistinguishable from a fresh one.
- `guard.py`'s check (not read in detail beyond confirming its existence via
  grep in pipeline.py's usage) matches on the `/.claudespace/` path segment,
  consistent with the doc's D5 claim that a nested `s/<instance>/` subtree
  would still be caught — not independently re-verified line-by-line since
  the doc's own reasoning (substring match survives added path depth) is
  self-evidently correct from the described mechanism.

## Affected Surfaces

Purely explanatory/confirmatory investigation — the design doc itself already
lists its own blast radius (`pipeline.py`, `backends/common.py`,
`backends/tmux.py`, `backends/iterm.py`, `handoff.py`, `watchdog.py`,
`workspace.py`, 6 `assets/prompts/*.prompt.md` files). No additional consumers
were found beyond what the doc names; this brief does not itself propose or
imply an implementation change, so no new affected-surface analysis is added.

## Existing Implementation & Placement

**Existing implementation**: None. The scoping mechanism described (`s/`
subtree, `CLAUDESPACE_MARKER_DIR`, `session_marker_dir` helper, instance on
`Window`, `.nagged` mtime check) does not exist anywhere in the codebase
today — verified by grep (no `CLAUDESPACE_MARKER_DIR`, no `session_marker_dir`,
no `instance` field on any `Window`/`TmuxWindow` dataclass).

**Correct home**: This is internal `claudespace` package infrastructure
(session/pipeline plumbing), not application/product code — there is no
shared/upstream package boundary question here. The doc's own "Blast radius"
section already correctly scopes every touched file to this repo's
`claudespace/` package and `assets/prompts/`.

## Execution Flow

Not applicable — this is infrastructure/design review, not a runtime request
flow.

## Relevant Files

- `claudespace/pipeline.py` — marker path builders, `resolve_root`,
  `MARKER_DIR`; confirms no per-instance scoping today.
- `claudespace/backends/common.py` — `launch_command_text`; confirms current
  env exports and absence of `CLAUDESPACE_MARKER_DIR`.
- `claudespace/backends/tmux.py` — `TmuxWindow`, `find_workspace`,
  `@cs_instance` handling; confirms instance is pane-lookup-only today.
- `claudespace/backends/iterm.py` — mirrors tmux.py's instance handling for
  iTerm2; confirms same gap.
- `claudespace/backends/base.py` — `Window` protocol; confirms no `instance`
  field.
- `claudespace/handoff.py` — `.nagged` sentinel logic; confirms pure
  presence check, no mtime scoping.

## Relevant Components

- Pipeline marker path builders (`pipeline.py`)
- Terminal backends (`backends/tmux.py`, `backends/iterm.py`, `backends/common.py`)
- Handoff/nag bookkeeping (`handoff.py`)

## Existing Constraints

- `CLAUDESPACE_INSTANCE` is fixed for a workspace's life and re-exported
  identically across tmux-resurrect restores (per doc's claim; consistent
  with `_session_name` encoding the instance suffix into the tmux session
  name, tmux.py:212-214).
- `resolve_root` must be called before any marker path is built, everywhere,
  since it's the only worktree-redirection point.

## Existing Behaviour

Marker collisions and stale-nag leakage described in the design doc are real
and reproducible from current code as read: two sessions on one repo share
identical `<root>/.claudespace/<role>.done` paths, and `.nagged` has no
run-scoping.

## Unknowns

None — the design doc is self-contained and its factual claims were all
verified against current source.

## Routing

This is a proposed design document being investigated for accuracy, not a
fresh feature request. No product ambiguity exists (the doc already states
its own goal, non-goal, layout, and decisions D1–D5). Per the fast-path
rules, this routes to **principal** directly — skipping planner — since the
doc already reads as a well-scoped engineering design; principal can turn it
into an implementation design or confirm it's implementation-ready as-is,
without needing planner to restate facts already settled here.
