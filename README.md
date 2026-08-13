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

claudespace isn't just a window layout - it's a five-role software delivery
pipeline (researcher → planner → principal → implementer → reviewer), each
role running as its own Claude Code session with its own model, its own
system prompt, and a narrow mandate it isn't allowed to step outside of. The
roles hand work to each other automatically, on disk, through small marker
files a Stop hook watches for - no copy-pasting context between panes.

## Table of contents

- [How it works](#how-it-works)
- [Where do I start?](#where-do-i-start)
- [Platform support](#platform-support)
- [Requirements](#requirements)
- [Install](#install)
- [Usage](#usage)
- [Pipeline handoff](#pipeline-handoff)
- [Unattended multi-feature runs (`agentic` template)](#unattended-multi-feature-runs-agentic-template)
- [Adding your own template](#adding-your-own-template)

## How it works

Every role is a Claude Code session running one of the prompts in
`claudespace/assets/prompts/`, invoked via its matching slash command
(`/researcher`, `/planner`, `/principal`, `/implementer`, `/reviewer`,
`/conductor`). Each role reads only the artifacts it needs, produces exactly
one artifact of its own, and stops - it never reaches forward or backward
into another role's job.

| role | question it answers | reads | produces | never does |
|---|---|---|---|---|
| **researcher** | How does this work *today*? | the request, existing docs | Technical Brief (facts, execution flow, files, unknowns) | design, implement, review, speculate |
| **planner** | What should we build, from a product standpoint? | the request, product notes | Planning Brief (scope, requirements, acceptance criteria) | read code, design architecture |
| **principal** | How should we build it? | Planning Brief + Technical Brief | Implementation Design (architecture, data flow, migrations, implementation order) | redefine requirements, write code |
| **implementer** | Build it. | Implementation Design | working code, tests, an implementation report | redesign, add unrelated scope |
| **reviewer** | Is it actually done and correct? | Implementation Design + the diff | a review with a PASS / CHANGES REQUIRED verdict | fix issues itself, invent new requirements |

Each artifact is a real file, persisted to wherever your project's own
documentation conventions say it should live (`docs/research/`,
`docs/design/`, or whatever `CLAUDE.md` specifies) - `claudespace` never
invents a parallel copy. The only thing living under `.claudespace/` is
small routing state: `<role>.done` / `<role>.blocked` marker files whose
*content* is the path to the real artifact.

### How the roles talk to each other

The default path is linear, but it isn't the only path - a role that hits a
blocker mid-stage doesn't have to wait for the whole pipeline to finish and
loop back around:

```
                    ┌──────────┐
        ┌──────────▶│ planner  │◀───────────────────┐
        │           └────┬─────┘                    │
        │                │ Planning Brief            │ product-scope question,
        │                ▼                           │ forwarded from implementer
        │           ┌──────────┐   rejects a    ┌─────┴──────┐
  Technical          │ principal│──vague Plan───▶│ bounce to  │
  Brief              └────┬─────┘   Brief        │  planner   │
        │                │ Implementation          └────────────┘
        │                │ Design                        ▲
┌──────────┐        ┌────────────┐   design/arch          │
│researcher│───────▶│ implementer│───question─────────────┘
└──────────┘        └─────┬──────┘   (bounce to principal)
   │   ▲                  │ code + report
   │   │ trivial fix,      ▼
   │   │ no design    ┌──────────┐   CHANGES REQUIRED
   │   └──────────────│ reviewer │──▶(bounce to implementer)
   │   skip straight  └────┬─────┘
   │   to implementer      │ PASS
   │                       ▼
   │                  you (terminal) — or, under `conductor`,
   │                  back to conductor for the next backlog item
   └── skip planner: researcher → principal directly
       (well-scoped engineering change, no open product question)
```

- **Forward, on success** — researcher → planner → principal → implementer
  → reviewer. Reviewer's PASS is terminal by default: it surfaces to you
  and nothing auto-advances, so a run never quietly keeps going past your
  blind spot.
- **Fast paths past the default** — researcher can route straight to
  **principal** (skipping the Planning Brief) when a change is a
  well-scoped engineering task with no open product question, or straight
  to **implementer** (skipping both Planning Brief and Implementation
  Design) when the fix is trivial and there's exactly one reasonable way to
  make it. Implementer can still escalate back to principal on its own if a
  "trivial" fix turns out to need real design work once it starts digging.
- **Rejections** — principal can bounce a whole Planning Brief back to
  planner if it's too ambiguous to design against; reviewer can bounce a
  whole implementation back to implementer on `CHANGES REQUIRED`. Both
  redo the artifact from scratch, not just patch it.
- **Questions, not rejections** — implementer, stuck on something only an
  upstream role can resolve, can ask a single targeted question without its
  own work being thrown out: **principal** for a design/architecture
  question, **planner** for a product-scope question. Principal can also
  forward a question on to planner if it turns out to be product-scoped
  rather than architectural. Whoever answers routes back to *whoever
  asked* - not forward along the fixed pipeline - so an answered question
  resumes exactly where it was asked.
- **Conductor** (optional sixth role, see below) sits outside this chain
  entirely - it only decomposes a goal into a backlog and dispatches one
  item at a time into the same researcher-first pipeline, picking up the
  next item automatically on every reviewer PASS.

All of this routing is driven by a single Stop hook
(`claudespace-handoff`) reading `pipeline.py`'s map of "who talks to whom" -
see [Pipeline handoff](#pipeline-handoff) for the mechanics.

## Where do I start?

**Start at `/researcher`, always** - even for a change you think is
trivial. Researcher is the one role allowed to read the repository before
anything else happens, and its job includes deciding how far the request
needs to travel through the rest of the pipeline:

```
claudespace                      # open the workspace (see Usage below)
```

Then, in the **researcher** pane:

```
/researcher add rate limiting to the /api/upload endpoint
```

From there:

- If it's a genuine product-facing feature with open scope questions,
  researcher hands off to **planner** - work through the pipeline pane by
  pane (or leave auto-handoff on, the default, see below, and mostly watch).
- If it's a well-scoped engineering change with no product ambiguity
  (a refactor, a dependency bump, an infra tweak), researcher skips
  straight to **principal**.
- If it's genuinely trivial (a typo, an off-by-one, a one-line config
  fix), researcher skips straight to **implementer**.

You never need to manually decide which pane to open first for a new
feature - open `/researcher` and let its routing decision carry you to the
right place. The only pane you invoke directly for a *new* request is
researcher (or `/conductor`, for a whole backlog of them - see
[Unattended multi-feature runs](#unattended-multi-feature-runs-agentic-template)).
Every other pane gets its work handed to it automatically.

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
with five small console-scripts (`claudespace-principal`,
`claudespace-implementer`, `claudespace-reviewer`, `claudespace-planner`,
`claudespace-researcher`) that each just launch `claude` pinned to a model
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
claudespace-sync-assets
```

### Updating

```
claudespace update
```

Pulls the latest claudespace from git into a temporary clone, reinstalls it
through pipx, and resyncs bundled commands/prompts - the same thing
`install.sh` does for a fresh install, minus the pipx/iTerm2 setup checks.

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
claudespace --template agentic   # unattended multi-feature run (auto-handoff is on by default), see below
claudespace --manual             # disable auto-handoff: press enter to advance each handoff
```

## Pipeline handoff

Each role's prompt writes its output to `.claudespace/<artifact>.md` in the
workspace root and, on completion, a `.done` marker. A globally-installed
Stop hook (`claudespace-handoff`, wired into `~/.claude/settings.json` by
`sync-assets`) watches for fresh markers and sends the next role's prompt
into its pane: researcher → planner → principal → implementer → reviewer.

By default, successful handoffs submit automatically. Pass `--manual` at
launch to only *prefill* the next pane's input instead - you press enter
to advance. Rejected or blocked work (principal bouncing a vague Planning
Brief back to planner, reviewer returning CHANGES REQUIRED to implementer)
always prefills only, regardless of auto-handoff - those always wait for
you.

### Bouncing questions, not just rejections

Two upstream roles can also be asked a targeted question mid-stage, without
their whole artifact being rejected:

- **implementer**, stuck on something only an upstream role can resolve,
  bounces to whichever one owns it - **principal** for a design/architecture
  question, **planner** for a product-scope question.
- **principal**, if a question implementer asked turns out to be
  product-scoped rather than architectural, can forward it on to
  **planner** itself.

Whoever answers routes back to *whoever asked*, not forward along the fixed
pipeline - principal or planner answering an implementer question hands
back to implementer directly, not to implementer's normal predecessor. This
uses the same `.blocked` marker mechanism as a rejection (see
`pipeline.py`'s `Stage.bounce_to`/`alt_next_roles`), so it inherits the same
always-prefill-only behavior regardless of auto-handoff.

The handoff's final submit keystroke is verified, not fire-and-forget: after
sending Enter, it polls the destination pane briefly to confirm the typed
prompt actually left the input box, and resends Enter (up to 3 attempts) if
a mid-repaint of claude's TUI swallowed it. This is what used to show up as
a handoff that silently stalled with the next prompt sitting typed-but-not-submitted,
requiring you to press Enter yourself.

Every workspace window is also tagged with a unique per-window instance ID,
not just its root folder path. Two `claudespace` windows opened against the
same root (e.g. two terminals working the same repo, or two worktrees that
resolve to the same real path) no longer risk a handoff in one window being
silently routed into a pane in the other - each hook only ever addresses
panes in its own window.

Add `.claudespace/` to your project's `.gitignore` - it's pipeline scratch
state, not something to commit.

## Unattended multi-feature runs (`agentic` template)

Everything above drives one unit of work through the pipeline per run - you
still re-trigger it for each feature. The built-in `agentic` template adds a
sixth pane, **conductor**, that turns a single high-level goal into a
backlog and drives the pipeline through it automatically, one item at a
time, until the backlog is done, blocked, or a run limit is hit:

```
claudespace --template agentic
```

Then in the conductor pane: `/conductor <describe the goal>`.

1. Conductor does a lightweight repo scan, decomposes the goal into an
   ordered backlog (`docs/backlog.md` by default - project doc conventions
   override this), and **stops** - this is the one mandatory checkpoint.
   Nothing is built yet.
2. Review/edit `docs/backlog.md` however you like - reorder items, delete
   ones you don't want, add a `checkpoint: true` line to any item you want
   the run to pause on after it passes review (default: none do). Resume
   conductor (e.g. `/conductor go`) once you're happy with it.
3. From here it's unattended: conductor dispatches the first eligible
   item to researcher, the normal pipeline runs it through
   planner/principal → implementer → reviewer, and on **PASS** conductor
   automatically picks up the next eligible item - no prompting, no
   pressing enter. `CHANGES REQUIRED` still bounces to implementer exactly
   as in a single-feature run.
4. The run stops and reports when the backlog is empty, every remaining
   item is blocked on an unmet `requires`, a `checkpoint: true` item just
   passed, or `--max-items` (default 5) is reached - whichever comes
   first. Re-invoke conductor to continue past a stop.

Panes stay long-lived, not one-shot - conductor's dispatch to researcher
between backlog items reuses the exact same clearing mechanism a human
starting a fresh `/researcher` request in an already-used workspace gets
(see "Pipeline handoff" above): every downstream pane (planner, principal,
implementer, reviewer) gets `/clear` sent into it once the previous item's
review actually passed, so feature N+1 starts each of those panes with a
clean conversation rather than accumulating every prior feature's context.
No terminal is ever quit or recreated - it's the same iTerm2 window and the
same underlying Claude Code sessions for the whole run, just periodically
cleared. Conductor's own pane is never cleared, since it has to remember
backlog state across every item in the run.

`--max-items N` bounds how many backlog items a single unattended run will
auto-advance through, regardless of backlog state - a circuit breaker
against a systemic issue (e.g. an overly lenient reviewer) compounding
silently across many features before you notice. It's prompt-enforced by
conductor itself, the same as every other pipeline instruction in this
project - not a hard code-level limit.

`agentic` relies on auto-handoff (on by default) to actually run
unattended; with `--manual`, every handoff (including conductor's own
dispatches) only prefills and waits for you to press enter, same as any
other template.

## Adding your own template

Templates and roles are just data - see `claudespace/config.py` for the
built-in ones (`native`, `opclaude`).

The easiest way to add your own is a TOML file at
`~/.config/claudespace/templates.toml` - no reinstall needed, and it
survives `claudespace update` since it lives outside the installed package.
One `[templates.<name>]` table per template, with a `layout` (must match a
name registered in `claudespace/layouts.py`) and one `[[templates.<name>.panes]]`
table per pane:

```toml
[templates.max]
layout = "main_left_grid_right"

[[templates.max.panes]]
role = "principal"
command = "claude2 --model claude-opus-4-8 --effort medium"

[[templates.max.panes]]
role = "implementer"
command = "claudespace-implementer"

[[templates.max.panes]]
role = "reviewer"
command = "claudespace-reviewer"

[[templates.max.panes]]
role = "planner"
command = "claude2 --model claude-opus-4-8 --effort medium"

[[templates.max.panes]]
role = "researcher"
command = "claudespace-researcher"
```

Each pane's `role` must match one of the roles the chosen layout produces
(`main_left_grid_right` needs exactly `principal`, `implementer`,
`reviewer`, `planner`, `researcher`). `command` is any shell command the
pane runs on open - a `claudespace-` console-script, `claude` with your own
flags, or something else entirely.

Run it with `claudespace --template max`; see all available templates
(built-in and user-defined) with `claudespace --list-templates`. A user
template with the same name as a built-in one overrides it.

Alternatively, add a `Template` directly in `claudespace/config.py` - it's
immediately available via `--template <name>`, but edits there are lost on
`claudespace update` since that reinstalls from a fresh clone.

## License

MIT
