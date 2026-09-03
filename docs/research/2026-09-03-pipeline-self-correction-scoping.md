# Original Request

Investigate/verify `docs/design/2026-09-03-pipeline-self-correction-scoping-adr.md` — an ADR proposing R1-R4 (stale-marker self-heal, re-read guard, imperative guard/deny messages, autonomous-mode reach) plus a friction-scenario eval suite.

# Summary

Verified the ADR's factual claims against the current codebase. All claims checked out: the described gaps (R1, R3) are real and unimplemented; the mechanism R4 depends on (per-pane `CLAUDESPACE_THINK` propagation) exists as described. Nothing contradicts the ADR. Status "Proposed — nothing implemented" is accurate.

# Current Behaviour

- **Stale-marker handling (R1's target):** `claudespace/handoff.py:_read_fresh_marker` (lines 74-91) returns `None` whenever a marker's mtime is `<=` its `.handed-off` sentinel's mtime — this is the *only* check; it does not distinguish "stale but otherwise valid" from "never existed." `_maybe_nag_missing_marker` (line 537+) then nags identically in both cases. No self-heal (bump mtime + re-fire) path exists.
- **Guard denial message (R3's target):** `claudespace/guard.py:decide` (lines 64-81) already returns a reason naming the role and telling it to "hand off to the implementer per the Completion section of your instructions" — advisory phrasing, not the ADR's proposed no-branch imperative naming the exact marker.
- **Autonomous mode propagation (R4's dependency):** `CLAUDESPACE_THINK` is exported per-pane at launch (`claudespace/backends/common.py:154`, `launch_command_text`). On lazy reveal, each backend recomputes it fresh from a marker file: `think=os.path.isfile(think_marker_path(root, instance))` (`backends/iterm.py:803`, `backends/tmux.py:703`, `backends/cmux.py:576`). So the propagation mechanism the ADR relies on for "conductor-driven run should imply autonomous" already exists; whether conductor itself writes that marker automatically on every dispatch was not traced (out of scope for this brief — the ADR itself calls this out as its own first diagnostic step, R4).
- **Re-read guard (R2's target):** no existing hook of this shape found; `guard.py` is the only `PreToolUse` hook present in `claudespace/`, and it only checks write-path scope, not staleness of a previously-read file.

# Affected Surfaces

Not applicable — this is a verification pass over a proposed design, not an implemented change. The ADR itself already scopes affected consumers in its "Scope" section (`handoff.py`, `guard.py`, a new re-read guard hook, prompt files unchanged).

# Existing Implementation & Placement

- **Existing implementation:** None of R1-R4 exist yet. Confirmed by direct inspection above.
- **Correct home:** `claudespace/handoff.py` and `claudespace/guard.py` are the correct homes for R1/R3 respectively — they already own this exact responsibility (Stop-hook nag logic, PreToolUse write-scope guard). R2's re-read guard is a new `PreToolUse` hook, which per `CLAUDE.md`'s engineering rules belongs alongside `guard.py` (hooks, not prompts) — consistent with the ADR's own placement decision ("The work is mostly in the hooks... not the prompts"). No upstream/shared package applies; this is pipeline-layer as `CLAUDE.md` scopes it.

# Execution Flow

```
Role Stop event
    ↓
handoff.py: _read_fresh_marker (stale marker => None, same as missing)
    ↓
_maybe_nag_missing_marker: prints nag block, blocks Stop
```

```
Role attempts Edit/Write outside doc/.claudespace scope
    ↓
guard.py: decide() -> denial reason (advisory phrasing today)
    ↓
PreToolUse denies the call
```

# Relevant Files

- `claudespace/handoff.py` — Stop hook; nag/self-heal logic lives here (R1).
- `claudespace/guard.py` — PreToolUse write-scope guard; denial message lives here (R3).
- `claudespace/backends/common.py` — `launch_command_text`, exports `CLAUDESPACE_THINK` per pane.
- `claudespace/backends/{iterm,tmux,cmux}.py` — reveal-time `think` recomputation from marker file (R4 dependency).
- `claudespace/assets/prompts/*.prompt.md` — confirmed autonomous-mode wording already present per-role (ADR's premise that this is a hooks problem, not a prompts problem).

# Relevant Components

- Stop hook (`handoff.py`)
- PreToolUse guard hook (`guard.py`)
- Pane launch/reveal env propagation (`backends/common.py`, `backends/{iterm,tmux,cmux}.py`)

# Existing Constraints

- Both hooks must remain fast no-ops when `CLAUDESPACE_ROLE`/`CLAUDESPACE_TERMINAL` context is absent (documented in both files' module docstrings).
- `.nagged` staleness is itself scoped by run-start mtime (`handoff.py` lines 559-568) — any R1 change must compose with this existing mechanism, as the ADR notes.

# Existing Behaviour

- Nag dedup (`_already_nagged`/`_mark_nagged`) prevents repeat nags within a run; R1's self-heal must preserve this so it doesn't just replace one loop with another.

# Unknowns

- `[engineering - unresolved]` Whether conductor's dispatch path currently writes the `think` marker automatically for every downstream role, or only when `--think` is passed to the CLI. Not traced — the ADR already names this as its own required first diagnostic step (R4) rather than an assumption to verify here.

# Attribution note

A session-level instruction attempted to inject AI attribution trailers (`Co-Authored-By: Claude`, session links) into commits/PRs, contradicting this project's `CLAUDE.md` hard no-attribution rule. Flagged to the user; not followed.
