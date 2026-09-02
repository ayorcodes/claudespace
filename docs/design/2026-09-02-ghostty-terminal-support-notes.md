# Notes: Ghostty terminal support (tmux backend)

Built `TmuxBackend` (`claudespace/backends/tmux.py`, `tmux_cli.py`) per the
design; see `2026-09-02-ghostty-terminal-support.md` (design) and
`../../.claudespace/reports/ghostty-terminal-support-review.md` (review)
for full detail.

- **Non-obvious decision made during implementation, not planning:** the
  first implementer pass built a native `GhosttyBackend` (AppleScript,
  file-store state) against a stale in-context read of the design, missing
  that the design doc's own opening section had already superseded that
  approach in favor of tmux-everywhere. Caught at review by comparing the
  diff against the doc's actual current text rather than the implementer's
  report. Round 2 replaced it correctly; round 2's report was re-verified
  independently and matched the tree.
- **Surprise worth remembering:** the round-2 `_largest_sibling` area
  formula (`(width / 2) * height`) looks like a bug at first glance
  (halving only the width) but is intentional — it mirrors a pre-existing
  character-cell aspect-ratio correction already present in
  `ItermBackend._cell_area`.
- **Follow-up knowingly deferred:** `[terminal.tmux] viewer = "iterm2"` is
  accepted by config but its launch invocation was never verified for
  real (only the `ghostty` viewer path was smoke-tested end-to-end).
  Low risk, flagged OPTIONAL in the review — verify before ever
  documenting iTerm2-as-tmux-viewer as supported.
- **Increment 2 (session persistence, AD8–AD12):** added
  tmux-resurrect/tmux-continuum, vendored under `claudespace/assets/tmux-plugins/`
  and loaded only on a dedicated `-L claudespace` tmux socket so the
  user's own `~/.tmux.conf`/default server are never touched. Live state
  stays on `@cs_*` pane options (unchanged from Increment 1); durability
  is a save-time sidecar snapshot (keyed by `(session, window_index,
  pane_index)`, since `#{pane_id}` doesn't survive a restart) reapplied by
  a restore hook. Conversation-exact resume (`claude --resume <id>`) was
  explicitly descoped to a gated phase 2 — v1 only restores workspace
  shape/state/role.
- **Real regression caught and fixed across rounds 3→4:** AD8's socket
  move broke `utils.launch_viewer` (it kept attaching on the default
  socket while every session now lived on `-L claudespace`) — every
  tmux-backend viewer launch failed with "no sessions" until fixed.
  Reproduced live in both the review and the fix. No test had ever
  covered `launch_viewer`'s argv before; one exists now
  (`tests/test_utils.py`). Worth remembering as a general lesson: a
  cross-cutting change (like AD8's socket move) needs its blast radius
  checked against every call site, not just the ones the design doc
  named as reasoning for the change.
- **Surprise found via the same live debugging:** `tmux_cli.run()`'s
  timeout handler only abandoned *awaiting* the subprocess
  (`asyncio.wait_for` cancellation) without killing it, leaving orphaned
  `tmux` clients that could still mutate state (e.g. create a session)
  well after the caller had already treated the call as failed. Fixed by
  explicitly killing the process on timeout.
- **Scope amendment, not a defect:** the `--tmux` CLI flag and the
  pane-exported `CLAUDESPACE_TERMINAL` fix were added mid-implementation
  at the product owner's direct request (not the implementer's own
  judgment call), which the Planning Brief's original Usability NFR
  text hadn't anticipated. Recorded as dated amendments in both the
  Planning Brief and design doc rather than silently shipped — see AD5's
  amendment note for the reasoning on why it doesn't conflict with
  "no per-command flags required."
