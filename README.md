# claudespace

Open a full Claude Code workspace in iTerm2 with one command: a `planner`,
`implementer`, `reviewer`, and `memory` pane, each pinned to a different
model, plus a scratch pane. Re-running against the same folder attaches to
the existing window instead of creating a duplicate.

```
┌────────────┬──────────────┬──────────────┐
│            │ implementer  │ memory       │
│  planner   ├──────────────┼──────────────┤
│            │ reviewer     │ scratch      │
└────────────┴──────────────┴──────────────┘
```

## Platform support

**macOS only.** claudespace drives iTerm2's official Python API, which has
no Windows or Linux equivalent — there is no cross-platform version of this
tool possible without swapping out the terminal entirely.

## Requirements

- macOS
- Python 3.12+
- [Claude Code](https://claude.com/claude-code) CLI, installed and on
  `PATH` (not installed automatically — see
  [claude.com/claude-code](https://claude.com/claude-code))

iTerm2 itself does **not** need to be pre-installed, and its Python API does
**not** need to be manually enabled — `claudespace` checks for both on
startup and handles them for you (see below).

## Install

```
curl -fsSL https://raw.githubusercontent.com/ayorcodes/claudespace/main/install.sh | sh
```

This installs [pipx](https://pipx.pypa.io) (via Homebrew) if you don't have
it, then installs claudespace through it in an isolated environment.

Or, if you already manage Python tooling yourself:

```
pipx install claudespace
# or
pip install claudespace
```

This installs the `claudespace` command plus four small console-scripts
(`claudespace:planner`, `claudespace:implementer`, `claudespace:reviewer`, `claudespace:memory`) that each just
launch `claude` pinned to a model - no shell config required.

| role          | model  |
|---------------|--------|
| planner       | opus   |
| implementer   | sonnet |
| reviewer      | opus   |
| memory        | haiku  |

### First-run setup

On first run, `claudespace`:

1. Checks the `claude` CLI is on `PATH` — exits with a link if not (this one
   it can't install for you, since it needs your login).
2. Checks iTerm2.app is installed — if not, and [Homebrew](https://brew.sh)
   is available, offers to run `brew install --cask iterm2` for you. Without
   Homebrew, it prints the manual download link instead.
3. Checks iTerm2's "Enable Python API" preference — if off, enables it via
   `defaults write` automatically. If iTerm2 was already running, you'll be
   asked to restart it once so the change takes effect.

## Usage

```
claudespace                 # build/attach a workspace for the current directory
claudespace --root ~/proj   # build/attach for a specific folder
claudespace --new           # force a new window even if one exists
claudespace --list-templates
```

## Adding your own template

Templates and roles are just data - see `claudespace/config.py`. Add a
`Template` with your own pane commands and it's immediately available via
`--template <name>`.

## License

MIT
