# Backlog: Produce a gitignored project-root CLAUDE.md for workspace-launcher

## project-claude-md
- status: done

Author a project-root `CLAUDE.md` capturing (a) this repo's own engineering
rules - macOS-only Python 3.12+ package, `pytest -q` + shellcheck CI, docs
conventions under `docs/{research,planning,design}`, backend-neutrality rules
(nothing outside `claudespace/backends/` imports `iterm2` or shells out to
`tmux`/`cmux`), test-per-module convention in `tests/` - and (b) an
architecture summary: the `backends/base.py` `TerminalBackend`/`Pane`/`Window`
split and `backends/get_backend()` selection, the iterm2/tmux/cmux backends
plus their `*_cli.py`/`tmux_persist.py` helpers, the pipeline/handoff/watchdog/
messaging layer, and `config.py`/`environment.py`/`layouts.py`/`themes.py`.
Requires its own scan of the modules above to decide what actually belongs.
Add `CLAUDE.md` to `.gitignore`; the file itself must not be committed.
Scope note: this is separate from the in-flight cmux-backend feature review
(see `.claudespace/s/7260adf4-518e-44a9-bff8-96cf8eea4e0b/reports/cmux-backend-review.md`,
"Round 2") - do not fold that review's findings into this item.
