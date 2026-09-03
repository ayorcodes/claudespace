# Notes: Per-session marker scoping

Every pipeline marker now nests under `<root>/.claudespace/s/<instance>/`
per the design; see `2026-09-03-per-session-marker-scoping.md` (design) and
`../../.claudespace/reports/per-session-marker-scoping-review.md` (review)
for full detail.

- **Round 1 → 2 blocker, caught by diffing the prompt text itself, not the
  implementer's grep:** the design's own verification grep
  (`grep -rn 'CLAUDESPACE_ROOT/\.claudespace' claudespace/assets/prompts/`)
  only matches the `$CLAUDESPACE_ROOT/`-prefixed form. `conductor.prompt.md`
  had 9 references to the `conductor-run`/`conductor.done` sentinels written
  as bare-relative paths (`.claudespace/conductor-run`, no prefix) — these
  slipped past that grep entirely and were left flat while
  `reviewer.prompt.md` and `pipeline.py::conductor_run_marker_path` were
  correctly scoped in the same commit. Net effect before the fix: conductor
  wrote the marker flat, reviewer/Python read it scoped, so the two sides
  never agreed and the conductor multi-item PASS-routing loop would have
  silently stopped advancing after the first backlog item — a regression
  worse than the pre-change behavior it was meant to fix. Worth remembering
  generally: a stated verification method (a specific grep pattern) can have
  a blind spot that matches its own substitution rule too narrowly; worth
  independently checking for the *class* of reference (any
  `.claudespace/<marker>` mention) rather than only the exact pattern named.
- **Round 1 → 2 blocker:** the `.nagged`-mtime fix — this design's other
  named purpose, the actual motivating bug (a stale sentinel from an earlier
  backlog item muting the nag for the next one) — shipped with zero test
  coverage in round 1 despite the design explicitly requiring
  `tests/test_handoff.py`. Added in round 2 with the three named cases plus
  a fourth for the no-existing-`.nagged` path.
- **Follow-up knowingly deferred:** none. Both rounds' fixes were scoped
  exactly to the two findings; no unrelated changes landed alongside them.
