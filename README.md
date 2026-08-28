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

Every role's pane is launched with its prompt from `claudespace/assets/prompts/`
already baked into its system prompt (via `--append-system-prompt-file`),
so the persona lives on the pane's process rather than in its conversation
- it's resent on every request and survives `/new`/`/clear` with nothing to
re-read. Pipeline handoffs into such a pane send plain continuation text
with no slash command at all. This applies automatically to any pane whose
role has a prompt file, regardless of what launches it - the built-in
`claude` commands and a custom command from your own template
(`~/.config/claudespace/templates.toml`) alike. claudespace appends the flag
to whatever the pane's command is before typing it in (see `iterm.py`'s
`_command_with_baked_persona`), so your template must not set it itself.
Because the persona is already in the system prompt, panes are launched with
no slash-command prefill at all - typing `/researcher` would only make the
model read a file it has already been given. The one thing that opts a pane out is a
role name with no matching prompt file (an unrecognized custom role) -
that pane falls back to the slash command (`/researcher`, `/planner`,
`/principal`, `/implementer`, `/reviewer`, `/conductor`) re-reading a
prompt file each handoff, same as before this feature existed. Note that a
custom command isn't guaranteed to actually be `claude` under a different
name - if it doesn't forward the appended flag through, or errors on an
unrecognized flag, that pane just fails to reach claude's ready prompt at
launch (a visible, debuggable error in that one pane, not silent
breakage - see `_wait_for_claude_prompt`). The matching slash command is
always how a human kicks off the first task in any pane. Each role reads
only the artifacts it needs, produces exactly one artifact of its own, and
stops - it never reaches forward or backward into another role's job.

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
- [Claude Code](https://claude.com/claude-code) CLI, installed, logged in,
  and on `PATH` (not installed automatically — it needs your login)

Python 3.12+ and iTerm2 are handled for you: `install.sh` finds a suitable
Python (installing one via Homebrew if it has to), and installs iTerm2 and
enables its Python API as part of the install rather than partway through
your first run. Re-check any of it at any time with `claudespace doctor`.

## Install

```
curl -fsSL https://raw.githubusercontent.com/ayorcodes/claudespace/main/install.sh | sh
```

The installer, in order: finds (or installs) Python 3.12+, installs
[pipx](https://pipx.pypa.io), installs claudespace into an isolated
environment, registers the bundled commands/prompts, then runs
`claudespace doctor` to sort out iTerm2 and its Python API. It finishes by
checking `claudespace` is actually on your `PATH` in a new shell, and tells
you exactly what to add if it isn't.

Each role runs `claude` pinned to a model and effort level. Those defaults
live in your `~/.config/claudespace/templates.toml`, so you can change any
of them without reinstalling:

| role        | model           | effort |
|-------------|-----------------|--------|
| conductor   | claude-opus-5   | medium |
| principal   | claude-opus-5   | medium |
| planner     | claude-opus-5   | medium |
| implementer | claude-sonnet-5 | medium |
| reviewer    | claude-sonnet-5 | medium |
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

### Setup checks (`claudespace doctor`)

`install.sh` runs this for you; run it yourself any time something looks
wrong. It:

1. Checks the `claude` CLI is on `PATH` — this one it can't install for
   you, since it needs your login.
2. Checks iTerm2.app is installed (including copies outside
   `/Applications`) — if not, and [Homebrew](https://brew.sh) is available,
   installs it. Without Homebrew, prints the manual download link.
3. Checks iTerm2's "Enable Python API" preference — if off, enables it via
   `defaults write`, then starts iTerm2 and waits for the API to actually
   come up, so you don't have to run anything a second time.

The one case that still needs you is iTerm2 already running when the
preference changes: it has to be restarted before the API is available.
If iTerm2 loads preferences from a custom folder, `doctor` says so and
points you at the GUI toggle instead of writing a setting iTerm2 ignores.

### Uninstalling

```
claudespace uninstall && pipx uninstall claudespace
```

Run `claudespace uninstall` **first**. It removes the global `Stop` hook
from `~/.claude/settings.json` and the bundled commands/prompts. Skipping it
leaves a hook pointing at a command that no longer exists, which then fails
on every turn of every Claude Code session on the machine. Your
`~/.config/claudespace/templates.toml` is left alone.

## Usage

```
claudespace                 # build/attach a workspace for the current directory
claudespace --root ~/proj   # build/attach for a specific folder
claudespace --new           # force a new window even if one exists
claudespace --list-templates
claudespace --template agentic   # unattended multi-feature run (auto-handoff is on by default), see below
claudespace --manual             # disable auto-handoff: press enter to advance each handoff
claudespace --think              # autonomous: planner decides open questions instead of asking you
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
follows the same toggle: auto-submitted by default, prefill-only under
`--manual`.

### `--think` (autonomous mode)

The planner normally stops and asks when a product question materially
affects scope or acceptance criteria. `claudespace --think` turns that off:
the planner still writes each question down, but answers it itself the way
a 30-year staff engineer at a top-tier shop would - conventional choice,
smallest blast radius, scope narrowed rather than widened - and records it
in the Planning Brief's **Assumptions** as `Q: ... -> A: ... (decided
autonomously)` so you can audit or reverse any single call later. Only
questions nobody could answer yet (business/legal/pricing, external
dependencies) stay in **Open Questions**, and the pipeline continues past
them.

The flag writes a `.claudespace/think` marker (also exported to each pane
as `CLAUDESPACE_THINK=1`), so it applies to an already-open workspace too:
re-run `claudespace --think` in the folder to switch the mode on, and a
plain `claudespace` run to switch it back off.

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
auto-handoff behavior described above.

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

### Watching for stalls (`claudespace watchdog`)

An unattended run's Stop hook only reacts when a pane actually finishes a
turn - a pane stuck behind a permission dialog, wedged in a runaway tool
loop, or whose `claude` process crashed outright never fires Stop, so
nothing notices it on its own.

```
claudespace watchdog --root .
```

runs alongside an open workspace (its own terminal, or backgrounded with
`nohup ... &`) and polls every pane's screen on an interval (`--interval`,
default 300s). A pane whose screen is unchanged for `--stall-after` seconds
(default 600s) *and* isn't sitting idle at claude's own prompt is flagged:
a macOS notification, a log line, and a `.claudespace/<role>.stalled`
marker (cleared automatically once that pane's screen moves again). Runs
until interrupted - there's no other exit condition, since a stuck run
might still be stuck whenever you next check.

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
command = "claude --model claude-opus-5 --effort high"

[[templates.max.panes]]
role = "implementer"
command = "claude --model claude-sonnet-5 --effort medium --permission-mode auto"

[[templates.max.panes]]
role = "reviewer"
command = "claude --model claude-sonnet-5 --effort medium --permission-mode auto"

[[templates.max.panes]]
role = "planner"
command = "claude --model claude-opus-5 --effort high"

[[templates.max.panes]]
role = "researcher"
command = "claude --model claude-sonnet-5 --effort low --permission-mode auto"
```

Each pane's `role` must match one of the roles the chosen layout produces
(`main_left_grid_right` needs exactly `principal`, `implementer`,
`reviewer`, `planner`, `researcher`). `command` is any shell command the
pane runs on open - `claude` with your own flags, a wrapper around a
different model or CLI, or something else entirely. claudespace appends
`--append-system-prompt-file` for the pane's role automatically, so the
command should not set it itself.

Run it with `claudespace --template max`; see all available templates
(built-in and user-defined) with `claudespace --list-templates`. A user
template with the same name as a built-in one overrides it.

Alternatively, add a `Template` directly in `claudespace/config.py` - it's
immediately available via `--template <name>`, but edits there are lost on
`claudespace update` since that reinstalls from a fresh clone.

## License

MIT
