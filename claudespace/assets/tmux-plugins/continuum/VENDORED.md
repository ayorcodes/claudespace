Vendored from https://github.com/tmux-plugins/tmux-continuum
Pinned commit: 0698e8f4b17d6454c71bf5212895ec055c578da0 (no tagged release
at vendoring time - HEAD of main as of 2026-09-02)

Trimmed to what claudespace's private tmux server actually loads: no
docs/, CHANGELOG.md, CONTRIBUTING.md, README.md.

To upgrade: re-fetch this subset from a newer commit/tag and update the
pinned commit above. Re-verify claudespace/backends/tmux.py's assumptions
against the new source before shipping - especially that autosave is still
driven by the status-right interpolation (client-attachment-dependent, see
tmux.py's module docstring) and that @continuum-boot stays unset/off so
handle_tmux_automatic_start's osx_enable.sh path is never exercised.
