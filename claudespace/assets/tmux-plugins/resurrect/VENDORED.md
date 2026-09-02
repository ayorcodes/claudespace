Vendored from https://github.com/tmux-plugins/tmux-resurrect
Pinned commit: e87d7d592cac97fa38c12395ebec042c154a1844 (tag v4.0.0)

Trimmed to what claudespace's private tmux server actually loads: no
docs/, tests/, video/, CHANGELOG.md, CONTRIBUTING.md, README.md, or
restore.exp (optional expect-based fallback, unused here). LICENSE.md kept.

To upgrade: re-fetch this subset from a newer tag and update the pinned
commit above. Re-verify claudespace/backends/tmux_persist.py's assumptions
against the new source (see its module docstring) before shipping -
especially that pane-scoped `@` user options still aren't captured by
`dump_panes_raw`'s pane_format, and that `post-restore-all` still fires
after restore_all_pane_processes in scripts/restore.sh's main().
