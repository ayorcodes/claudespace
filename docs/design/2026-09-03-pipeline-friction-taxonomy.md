# Pipeline friction taxonomy (manual eval checklist)

Status: **Active checklist**. Date: 2026-09-03.

Companion to `docs/design/2026-09-03-pipeline-self-correction-scoping-adr.md`
(decisions) and `docs/design/2026-09-03-pipeline-self-correction-impl.md`
(implementation, AD-4). `pytest` has no live model, so the deterministic half
of each row below is regression-guarded in CI (`tests/test_handoff.py`,
`tests/test_guard.py`); the model-in-loop half is not automatable and must be
checked manually/agentically against a live pipeline run.

For each row: reproduce the trigger in a real (or scripted) conductor/pipeline
run, and confirm the role takes the correct action **with no user turn**.

| Signal | Correct action | How to verify |
|---|---|---|
| Guard block (write outside scope) | Route to the owning role (write `.done` + `route: implementer`) in the same turn, never go dormant waiting for the user to say "route it" | Manual: trigger a blocked write from a read-only role pane, confirm it writes its `.done` marker unprompted. Deterministic: `test_guard.py::TestDecide::test_denial_is_imperative_and_names_the_marker` asserts the denial names the exact marker + `route: implementer`. |
| Stop-hook nag, marker stale-but-already-handed | System self-heals silently (no nag); role never sees an argument surface | Deterministic: `test_handoff.py::test_stale_but_already_handed_done_marker_is_not_nagged` / `..._blocked_marker_is_not_nagged`. |
| Stop-hook nag, marker genuinely missing | Role writes the marker once, no argument, no repeated rewrites | Deterministic: `test_handoff.py::test_genuinely_missing_marker_still_nags` (nag still fires) plus existing `.nagged` mtime-scoping tests (no double-nag). Manual: confirm the role writes once rather than rewriting the same marker repeatedly across turns. |
| Artifact changed since last read | Re-read before acting on it | Manual only (AD-2: the harness's native stale-`Edit`/`Write` protection is the covering mechanism; no claudespace-specific hook exists — this row stays manual until a concrete miss surfaces). |
| Design/architecture decision needed mid-implementation | Bounce to principal in the same turn (`.blocked` + `route: principal`), not silently pick an approach or stall | Manual: force a design-shaped ambiguity in an implementer run, confirm it bounces rather than guessing or stopping. |
| Permission deny under autonomous mode (`--think`) | Route or document-and-continue, never stop to ask the user | Manual: confirm `--think`/`CLAUDESPACE_THINK` is on for the run (AD-3's diagnostic), then trigger a guard deny and confirm the role proceeds per the imperative message rather than pausing. |

## Notes

- Rows already covered by unit tests are regression-guarded automatically;
  don't re-verify those by hand on every release, only when touching
  `handoff.py`/`guard.py`.
- If a manual check turns up a new recurring failure mode, add a row here
  first, then decide (per the ADR's own bar) whether it can move into a
  deterministic hook check or must stay manual.
