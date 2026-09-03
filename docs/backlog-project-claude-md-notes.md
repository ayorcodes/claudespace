# project-claude-md — notes

- Built: root `CLAUDE.md` (gitignored, not tracked) with engineering rules +
  architecture summary; `.gitignore` gained a `CLAUDE.md` line.
- Round 1 review caught the doc's own "backend neutrality" rule contradicting
  the code: `cli.py` directly imports/instantiates `TmuxBackend`/`CmuxBackend`,
  `themes.py` imports `iterm2`, `utils.py` shells to `tmux` — all outside
  `backends/`. Fixed by naming these as documented exceptions instead of
  claiming an absolute rule. See
  `.claudespace/s/7260adf4-518e-44a9-bff8-96cf8eea4e0b/reports/project-claude-md-review.md`
  for full evidence.
- Takeaway for future edits to `CLAUDE.md`: verify architectural claims
  against the code before writing them, especially "only place that..."-style
  absolutes — this file is loaded into every session's context, so a false
  claim actively misleads future work.
- Nothing deferred; no follow-up planned beyond the OPTIONAL wording nit in
  the review (imprecise attribution of one `cli.py` call site to "the viewer
  path" — not worth a dedicated pass).
