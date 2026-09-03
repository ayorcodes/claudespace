# Notes: CmuxBackend

Third `TerminalBackend`, driven through the `cmux` CLI. See
`2026-09-03-cmux-backend.md` (design), `2026-09-03-cmux-backend-scoping-adr.md`
(ADR), `../../docs/research/2026-09-03-cmux-backend-spike.md` (go/no-go
spike) and
`../../.claudespace/s/7260adf4-518e-44a9-bff8-96cf8eea4e0b/reports/cmux-backend-review.md`
(review) for full detail.

- **Passed on the first review round** — no BLOCKER/IMPORTANT findings.
- **Two disclosed, justified deviations from the design's literal text,**
  both caught by the implementer via a failing integration test rather than
  guessed: pane titles carry the *full* instance UUID (not the design's
  8-hex prefix) because cmux has no second identity channel the way tmux's
  `@cs_instance` option does; `reveal_role` splits directly off the handoff
  source rather than a "largest sibling" the design only speculated about,
  since cmux's `surface.list` exposes no pane geometry at all.
- **OPTIONAL, not acted on:** the design's Performance Considerations
  ("two `cmux rpc` reads per lookup, O(1)") doesn't actually hold for the
  instance-keyed hot path (`find_role_pane`/`each_pane`) — it scans every
  cmux workspace, not just this session's — but that's the design's Edge
  Cases section's own explicit tradeoff for cwd-drift immunity, not an
  implementer gap. Worth a look if cmux discovery latency ever becomes
  noticeable with many concurrent cmux workspaces open.
- **Follow-up knowingly deferred:** README's backend config example
  (`README.md`) still only mentions `tmux`/`iterm2`, not `cmux` — never
  named in the design's Components/Affected Surfaces, so it didn't block,
  but worth a small doc pass later.
- **Round 2 (post-PASS user feedback):** `--tmux` is a first-class
  per-invocation CLI override flag (both the main command and `watchdog`)
  that neither the design nor the ADR ever scoped an equivalent for on
  `cmux` — user caught the asymmetry directly ("we have `--tmux` why not
  `--cmux`?"). Added `--cmux` mirroring `--tmux` exactly, rejecting both
  flags set together as a usage error rather than silently picking one
  (commit `f2ba9fc`). Passed re-review with no new findings. Worth
  remembering generally: a same-invocation CLI override is part of the
  *user-facing contract* a new backend inherits by precedent, even when a
  design doc scopes only config-file/env selection — future backends
  should get one from the start.
