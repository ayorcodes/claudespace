# claudespace

Open a full Claude Code workspace in iTerm2 with one command: a `principal`,
`implementer`, `reviewer`, `planner`, and `researcher` pane, each pinned to
a different model and effort level. Re-running against the same folder
attaches to the existing window instead of creating a duplicate.

```
┌────────────┬──────────────┬──────────────┐
│            │ implementer  │ planner      │
│  principal ├──────────────┼──────────────┤
│            │ reviewer     │ researcher   │
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
it, then installs claudespace through it in an isolated environment, along
with five small console-scripts (`claudespace:principal`,
`claudespace:implementer`, `claudespace:reviewer`, `claudespace:planner`,
`claudespace:researcher`) that each just launch `claude` pinned to a model
and effort level - no shell config required.

| role        | model           | effort |
|-------------|-----------------|--------|
| principal   | claude-opus-4-8 | medium |
| implementer | claude-sonnet-5 | medium |
| reviewer    | claude-sonnet-5 | medium |
| planner     | claude-opus-4-8 | medium |
| researcher  | claude-sonnet-5 | low    |

### Bundled commands and prompts

`install.sh` also registers five global slash-commands - `/planner`,
`/principal`, `/researcher`, `/implementer`, `/reviewer` - by copying their
command files into `~/.claude/commands` and their prompt files into
`~/.ai/prompts`. Any pane opened by claudespace (or any other Claude Code
session on the machine) can use them right away. Existing files with the
same name are always overwritten with the bundled version, so re-running
the sync after an upgrade picks up fixes - any local edits to a prompt or
command will be lost. Re-run the sync manually with:

```
claudespace:sync-assets
```

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

## Pipeline handoff

Each role's prompt writes its output to `.claudespace/<artifact>.md` in the
workspace root and, on completion, a `.done` marker. A globally-installed
Stop hook (`claudespace:handoff`, wired into `~/.claude/settings.json` by
`sync-assets`) watches for fresh markers and sends the next role's prompt
into its pane: researcher → planner → principal → implementer → reviewer.

By default handoffs only *prefill* the next pane's input - you press enter
to advance. Pass `--auto-handoff` at launch to have successful handoffs
submit automatically. Rejected or blocked work (principal bouncing a vague
Planning Brief back to planner, reviewer returning CHANGES REQUIRED to
implementer) always prefills only, regardless of `--auto-handoff` - those
always wait for you.

Add `.claudespace/` to your project's `.gitignore` - it's pipeline scratch
state, not something to commit.

## Adding your own template

Templates and roles are just data - see `claudespace/config.py`. Add a
`Template` with your own pane commands and it's immediately available via
`--template <name>`.

## License

MIT
