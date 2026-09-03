# Implementation Design: Pipeline self-correction (R1–R5)

Status: **Ready for implementation**. Date: 2026-09-03.

# References

- ADR (decisions, rationale, scope): `docs/design/2026-09-03-pipeline-self-correction-scoping-adr.md`
- Technical Brief (verified current behaviour): `docs/research/2026-09-03-pipeline-self-correction-scoping.md`
- Composes with: `docs/design/2026-09-03-per-session-marker-scoping.md` (stale-`.nagged` mtime scoping)

The ADR owns the "what" and "why"; this doc is the "how". Read it first.

# Architecture Decisions

The ADR fixes the high-level decisions. This design resolves the engineering
uncertainty the ADR left open and, in three places, diverges from its literal
wording with justification. All divergences are also captured under **Open
Questions** (autonomous-mode decisions, auditable/reversible).

### AD-1 — R1 is *nag suppression*, not "bump mtime + re-fire"

The ADR proposes the Stop hook refresh a stale marker and re-fire the handoff.
Rejected in favour of: **when a marker is present-but-stale AND already carries
its `.handed-off` sentinel, treat it as "already handed off — nothing to do"
and exit silently (no nag, no re-send).**

Reasoning:
- A `.handed-off` sentinel is only ever written by `_mark_handed_off`, which
  runs *after* `_send_handoff` completed a successful backend send
  (`handoff.py:529`). So a stale-but-valid marker with a sentinel is proof the
  send already landed. Re-firing would type the handoff prompt into the
  downstream pane a second time.
- "Bump mtime + re-fire" also risks a genuine loop: the re-fire calls
  `_mark_handed_off` again, leaving the marker stale again, so the next Stop
  re-heals — needing a second sentinel to break. Suppression needs no new
  sentinel and cannot loop.
- The failure the ADR targets (scenario 2) is the *nag* the model argued with.
  Removing the nag for the already-handed case removes the argument surface
  directly — "the model never gets the chance to argue" (ADR R1), achieved by
  the nag never firing rather than by out-racing the model.

Genuinely-missing markers still nag (unchanged). This stays strictly gated per
the ADR: present **and** already-handed → silent; missing → nag.

### AD-2 — R2's standalone re-read guard hook is deferred; the ground-truth rule ships

The ADR's R2 has two parts: a new `PreToolUse` re-read guard, and a standing
"Stop-hook state is ground truth" rule.

- **Ground-truth rule: ship.** Realised as message wording the hooks already
  emit (R1/R3), asserting the state is authoritative — no new prompt paragraph.
- **Standalone re-read guard hook: defer.** A `PreToolUse` hook only sees tool
  calls, not the model reasoning over a stale in-context copy of an artifact —
  which is the actual scenario 3 failure. The one case it *could* catch (an
  `Edit`/`MultiEdit` against a file changed on disk since last read) is already
  covered by the Claude Code harness's native stale-file protection (`Edit`
  fails when the file changed since the last Read). A separate hook would add a
  stateful cross-process read-mtime ledger for near-zero incremental coverage —
  against R5's "do not over-correct". Deferred pending evidence of a case the
  native protection misses.

### AD-3 — R4 needs no code change beyond R3

Diagnostic (ADR's required first step) resolved by inspection:
- `--think` **defaults on** (`cli.py:433–438`); `--manual` explicitly opts out
  and wins over `--think` by design. The think marker is written by
  `workspace._set_think` at workspace-open and read per-pane at launch/reveal
  (`backends/*.py`).
- A conductor-driven run launched normally therefore already has autonomous
  mode on. Forcing conductor to override an explicit `--manual` is rejected: it
  overrides the user's deliberate conservative choice and violates R5.
- The block/deny "route, don't stop" behaviour the ADR wants from R4 is
  delivered **mode-independently** by R3's imperative hook message — a better
  outcome than gating it on autonomous mode. No new code for R4.

### AD-4 — the "eval suite" is deterministic hook tests + a manual taxonomy doc

`pytest` has no live model, so a friction eval that asserts "a role facing a
wall takes the right recovery with no user turn" cannot run end-to-end in CI.
Split:
- **Deterministic recovery, in CI:** unit tests over `handoff.py` (R1) and
  `guard.py` (R3) asserting the hook's own output is the correct recovery. This
  is where the recovery now *lives* (moved out of model judgment per the ADR),
  so it is exactly what regression-guards the property.
- **Model-in-loop scenarios:** captured as a checklist in the taxonomy doc for
  manual / agentic eval, not wired into `pytest`.

Correct home for all changes: pipeline layer (`handoff.py`, `guard.py`,
`tests/`), per `CLAUDE.md`. No backend-specific work. Confirmed by research.

# Components

- `claudespace/handoff.py` — Stop hook. R1 (nag suppression for already-handed
  stale markers).
- `claudespace/guard.py` — PreToolUse write guard. R3 (imperative denial naming
  the exact marker + route).
- `claudespace/pipeline.py` — no change; provides `done_marker_path` /
  `blocked_marker_path` reused by R3.
- `tests/test_handoff.py`, `tests/test_guard.py` — extended (R1/R3 assertions).
- `docs/design/2026-09-03-pipeline-friction-taxonomy.md` — new manual eval
  checklist (AD-4).

# Data Flow

**R1 (Stop hook, stale-marker case):**
```
Role Stop → handoff.main → _run
  _send_handoff → _read_fresh_marker == None (stale) → returns False
  _maybe_nag_missing_marker:
     present-and-handed(done|blocked)?  ── yes ─→ return False (silent exit)
                                         ── no  ─→ existing missing-marker nag
```

**R3 (PreToolUse, blocked write):**
```
Edit/Write outside scope → guard.decide(role in READ_ONLY_ROLES)
  → imperative reason: "Do NOT stop. Next and only action:
     write <done_marker> with `route: implementer` naming a .md note. Then stop."
  → permissionDecision: deny
```

# API Changes

None (console-script entrypoints and signatures unchanged).

# Database Changes

None.

# Validation

- R1: only suppress when the marker file **exists** and its `.handed-off`
  sentinel exists with `mtime(sentinel) >= mtime(marker)` (i.e. exactly the
  stale-already-handed state). Any other state (missing sentinel, marker newer)
  is *not* suppressed — it falls through to the existing fresh-handoff or nag
  paths untouched.
- R3: read `CLAUDESPACE_ROOT` / `CLAUDESPACE_INSTANCE` from env. If `root` is
  absent (hook running outside a pane — already a guarded no-op path), emit the
  imperative wording *without* a concrete marker path rather than crashing.

# Error Handling

- R1 helper does pure `os.path` stat checks; a missing sentinel is a normal
  `False`, never an exception. Preserves the module's "fast no-op on missing
  context" contract.
- R3: `done_marker_path` is pure string building; safe. Guard still exits
  silently (allowing the call) on any unexpected error per existing `main`.

# Security Considerations

R5 preserved: the guard still **denies** out-of-scope code writes — R3 only
rewrites the *message*, never widens what is allowed. No self-heal ever
approves an irreversible or security-relevant action; R1 only suppresses a nag
for work already handed off.

# Performance Considerations

Both hooks fire once per Stop / per write tool call. R1 adds at most two
`os.path.isfile` + two `os.path.getmtime` calls (bounded, local FS). R3 adds
one string build. No collection scans, no N+1, no query concerns — there is no
datastore.

# Compatibility

- Fully backward compatible. R1 only changes behaviour in the already-handed
  stale case (previously a spurious nag → now silent); every other path is
  byte-identical.
- R3 changes only human/model-facing message text; the `deny` decision is
  unchanged, so no downstream tooling contract shifts.
- No marker format change, so in-flight runs and older panes are unaffected.

# Edge Cases

1. **Stale done + stale blocked both present-and-handed** → suppress (either
   qualifies). Bounce (`.blocked`) only counted when `stage.bounce_to` is set.
2. **Marker present but no `.handed-off` sentinel** (role wrote it, hook has not
   yet processed it, or send failed) → NOT suppressed. Fresh path or error-nag
   handles it. Correct: we must not silence a marker that never handed off.
3. **Marker newer than sentinel** (role legitimately rewrote it with new
   content) → `_read_fresh_marker` returns fresh → normal handoff. Untouched.
4. **reviewer terminal PASS, no conductor-run** → `next_role is None`, not
   nag-eligible today; R1 adds no new nag, so unchanged.
5. **R3 outside a pane** (`CLAUDESPACE_ROLE` unset) → guard already returns
   `None` (allow) before reaching the message. No regression.
6. **R3 role whose only implementer route is `.blocked` vs `.done`** →
   all four `READ_ONLY_ROLES` can reach `implementer` (principal via
   `next_role`; researcher/planner/reviewer via `alt_next_roles`). Message names
   the `.done` marker + `route: implementer` (forwarding a code change, not a
   bounce), consistent with "Handing off work that isn't yours".

# Tests Required

Unit (extend existing modules, one-per-source-module convention):

`tests/test_handoff.py` (R1):
- stale done marker + `.handed-off` present → `_maybe_nag_missing_marker`
  returns `False` and writes **no** `.nagged`.
- stale blocked marker + `.handed-off` (bounce stage) → `False`, no nag.
- marker present, **no** `.handed-off` → still nags (regression guard for
  edge case 2).
- genuinely missing marker → still nags (unchanged).

`tests/test_guard.py` (R3):
- blocked write for each `READ_ONLY_ROLES` role → reason contains `Do NOT
  stop`, the role's `.done` marker path, and `route: implementer`.
- `CLAUDESPACE_ROOT` unset → reason still returned, no concrete path, no crash.
- allowed paths (`.md`, inside `.claudespace/`) → still `None` (unchanged).

Integration/e2e: none new (no live-model harness in CI — see AD-4).

# Verification

```
pytest -q
pytest -q tests/test_handoff.py tests/test_guard.py
shellcheck -s sh install.sh   # unchanged, but part of CI gate
```

# Implementation Order

1. **R1** — add `_marker_present_and_handed(path)` helper in `handoff.py`; call
   it in `_maybe_nag_missing_marker` after the existing fresh-marker check, for
   both `done_path` and (bounce) `blocked_path`; return `False` when either is
   present-and-handed. Update the function docstring.
2. **R1 tests** — extend `tests/test_handoff.py` per above. Run.
3. **R3** — rewrite `guard.decide`'s denial string to the imperative form;
   import `done_marker_path` from `claudespace.pipeline` and read
   `CLAUDESPACE_ROOT`/`CLAUDESPACE_INSTANCE` (pass role/root/instance into
   `decide`, or read env inside it — keep `decide` unit-testable by accepting
   optional `root`/`instance` args defaulting to env lookup). Preserve the
   no-path fallback.
4. **R3 tests** — extend `tests/test_guard.py`. Run.
5. **AD-4 doc** — write `docs/design/2026-09-03-pipeline-friction-taxonomy.md`
   as the manual friction checklist (the ADR's table, one row per signal →
   correct action → how to verify).
6. `pytest -q` green; `shellcheck` clean.

Prompt edits (R4 wording) are intentionally **not** in this sequence — R3's
mode-independent hook message supersedes them (AD-3). If a maintainer still
wants the explicit autonomous-mode "route, never stop" line, it is a one-line
addition to each role prompt's Autonomous section, independent of this work.

# Open Questions

- Q: R1 literal "bump mtime + re-fire" vs. nag suppression? → A: suppress the
  nag for already-handed stale markers; do not re-fire (avoids double-send and
  a heal-loop; `.handed-off` proves the send already landed). *(decided
  autonomously)*
- Q: Build R2's standalone re-read guard hook? → A: defer it; ship only the
  ground-truth rule via hook wording. The harness's native stale-`Edit`
  protection already covers the tractable case; a PreToolUse hook cannot see
  reasoning over stale in-context artifacts. Revisit if a concrete miss
  appears. *(decided autonomously)*
- Q: R4 — force conductor-driven runs to autonomous even under `--manual`? →
  A: no. `--think` already defaults on; `--manual` is a deliberate opt-out that
  must win. R3's imperative message delivers the block/deny recovery
  mode-independently, so no R4 code change. *(decided autonomously)*
- Q: Friction eval in CI? → A: deterministic hook-output assertions in
  `pytest`; model-in-loop scenarios as a manual taxonomy doc (no live model in
  CI). *(decided autonomously)*
