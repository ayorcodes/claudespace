# ADR: Pipeline self-correction — recover from friction without the user (scoping)

Status: **Proposed** — nothing implemented. A companion friction-scenario eval
plan is part of this scope (see "Making it stick").

Date: 2026-09-03

## Context

In a live conductor run, the pipeline repeatedly stalled in ways that required
the user to re-prompt, when the correct recovery was already defined by the
system:

1. **Guard block → dormancy.** `researcher` tried to edit a non-doc file
   (`~/.config/cmux/cmux.json`); `guard.py` correctly blocked it. Instead of
   routing the edit to `implementer`, the role went dormant and waited. The
   user had to say "route it."
2. **Stale marker → argue-loop.** `implementer`/`researcher` insisted from
   memory that a marker was "already written / already routed," while the Stop
   hook nagged that no *fresh* marker existed (its mtime predated its
   `.handed-off`). The role argued with the hook and rewrote the same marker
   several times instead of just refreshing it — a near handoff-loop.
3. **Didn't re-read a changed file.** A role acted on an artifact from its
   in-context memory after that file had changed on disk, contributing to the
   loop.
4. **Needless "OK to proceed?" stops** on reversible, in-scope actions.

**Key finding: the guidance already exists.** `guard.py`'s block message
already says "hand off to the implementer per the Completion section." The
prompts already say "never just explain why it's out of scope and wait to be
told where to send it," already say "rewrite the marker file itself, a fresh
write even if identical — the Stop hook only re-sends when its mtime is newer,"
and already forbid addressing the user in autonomous mode. The roles still
failed to follow them.

So this is **not a missing-instruction problem. It is an adherence-under-
friction problem** — and writing more prompt text will not fix a text-
adherence failure.

### The shared root cause

Every incident is the same shape: **when a role hits a wall (guard block,
Stop-hook nag, a changed file), it defaults to narrate / stop / trust its own
memory, instead of taking the deterministic recovery the system already
defines.** Recovery lives in the model's *judgment*, exercised many turns and
hundreds of prompt-lines away from the rule that covers it, so it is
unreliable exactly when it's needed.

## Decision (proposed)

**Move recovery out of the model's judgment and into the system's enforced
responses.** Where a recovery is provably safe, the system takes it and the
model is never in the loop; where it isn't, the system's response is an
imperative next action, not advice. Lock the result in with an eval suite over
the recurring friction scenarios.

The work is mostly in the **hooks** (`handoff.py`, `guard.py`, a new re-read
guard), not the prompts — because the prompts already say the right thing.

## Decisions in detail

### R1 — Self-heal the stale-marker case in the Stop hook (kills the loop)

When the nag fires, `handoff.py` can already see everything: whether a marker
file exists, whether its content is a valid `route:`, and that it is merely
*stale* (mtime ≤ its `.handed-off`). In that exact case the hook should
**refresh the marker (bump mtime) and re-fire the handoff itself**, rather than
emitting a nag for the model to act on. The model never gets the chance to
argue. Strictly gated: file present **and** route valid **and** only-stale;
a genuinely-missing marker still nags. Composes with the stale-`.nagged`
mtime fix already scoped in
`docs/design/2026-09-03-per-session-marker-scoping.md`.

### R2 — Ground-truth-over-memory, enforced

- **Re-read guard (new hook).** Mirror this harness's own "file changed on
  disk since you last read it" behavior, which claudespace roles do not get: a
  `PreToolUse` hook that, when a role acts on an artifact whose mtime changed
  since it last read it, injects "re-read first." Kills the "acted on a stale
  file" failure directly.
- **Standing rule.** A Stop-hook statement about pipeline state (marker
  missing/stale) **is** ground truth: comply, never rebut from memory. Phrased
  as a rule the hook's own message asserts, not a new prompt paragraph to be
  forgotten.

### R3 — Blocks and denials read as imperatives, not footnotes

`guard.py`'s denial (and, where reachable, a permission denial) should name the
exact recovery with no branch: *"Do NOT stop. Your next and only action: write
`<marker>` with `route: implementer` naming a note describing the change. Then
stop."* The guard already knows the role and path; it can name the marker. A
block is a routing instruction, never a dead-end.

### R4 — Ensure autonomous mode is on, and extend its reach to friction

Several stops are exactly what `--think` / `CLAUDESPACE_THINK` exists to
suppress, so either it was off on that run or its coverage doesn't reach
block/deny/nag moments. A conductor-driven run should imply autonomous, and the
autonomous rule should state explicitly: *a guard block or permission deny
under autonomous mode = route immediately, never stop.*
**First, cheap diagnostic step: confirm whether `--think` was set on the
failing run** — if not, some of this was working-as-designed and the real bug
list is shorter.

### R5 — Preserve the legitimate stops (do not over-correct)

The enemy is dormancy, argue-loops, and stale-state assertions — **not** every
stop. The `cmux.json` socket-widening was a genuine security-relevant change;
pausing for sign-off there was correct (the harness's own classifier blocked it
too). Each role keeps a crisp "these are the only reasons to actually stop"
allowlist (conductor already has "Stopping conditions"); everything else is
route-or-decide. Irreversible / security-relevant / genuine product-ambiguity
stops stay.

## Making it stick: a friction-scenario eval suite

These failures only surfaced in production because **nothing tests that a role
facing a given wall takes the right recovery.** Scope a small "friction
taxonomy," each entry = *triggering signal → single correct action → eval*:

| Signal | Correct action |
|---|---|
| Guard block (write outside scope) | route to owning role, same turn |
| Permission deny (autonomous) | route or document-and-continue, never stop |
| Stop-hook nag, marker stale | (system self-heals; role otherwise rewrites once, no argument) |
| Artifact changed since last read | re-read before acting |
| Design/architecture decision needed | bounce to principal, same turn |

Each becomes a fixture; the eval asserts the recovery happens with **no user
turn**. This turns "proper agentic" from a vibe into a measured, regression-
guarded property, and stops the next prompt tweak from silently breaking it.

## Scope

**In scope**
- R1 stale-marker self-heal in `handoff.py`.
- R2 re-read guard hook + ground-truth rule.
- R3 imperative guard/deny messages.
- R4 autonomous-mode-implies-conductor + friction-event coverage, after the
  `--think` diagnostic.
- The friction-taxonomy eval suite.

**Out of scope (explicitly)**
- Rewriting the pipeline shape, roles, or routing model — the routing paths are
  correct; only recovery *adherence* is broken.
- Auto-approving irreversible or security-relevant actions (R5).
- Auto-writing a *new* marker when none exists or the route is unknown — the
  hook only refreshes a valid, stale one (R1).
- Anything backend-specific (tmux/iterm2/psmux/cmux) — this is pipeline-layer.

## Consequences

**If it works out**
- The four observed failures resolve without a user turn; the loop becomes
  impossible (R1 removes the model from it) and stale-state assertions are
  caught (R2).
- Recovery behavior is enforced and evaluated, not re-authored per incident.

**Risks accepted / to watch**
- **Over-automation.** A too-aggressive self-heal or auto-route could paper
  over a real problem the user should see. Mitigated by R1's strict gate, R5's
  preserved-stops allowlist, and the fact that every self-heal is logged and
  auditable after the fact.
- **Hook-injected text is still text.** R2/R3 make the system's *message*
  imperative, but the model can still ignore it; that residue is exactly what
  R1 (deterministic, model-out-of-loop) and the evals exist to bound.
- **Autonomous default** could surprise a user who wanted a supervised run —
  keep the non-`--think` path fully human-in-loop; R4 only tightens the
  already-autonomous case.

## References

- Triggering run: cmux spike/backend work (`docs/research/2026-09-03-cmux-backend-spike.md`).
- Stale-`.nagged` mtime fix + file-homed state: `docs/design/2026-09-03-per-session-marker-scoping.md`.
- Mechanisms: `claudespace/handoff.py` (Stop hook, nag), `claudespace/guard.py` (write guard), `claudespace/assets/prompts/*.prompt.md` (routing/autonomous rules already present).
