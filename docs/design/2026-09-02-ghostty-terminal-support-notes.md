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
