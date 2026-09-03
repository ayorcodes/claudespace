# Original Request

> we need to update the install script to now force iTerm download especially
> if tmux, ghostty is available, we need a way to check for support for what
> we support then we can now suggest to download if nothing available,
> example, someone deleted ghostty, and doesn't have Iterm2, but they have
> cmux, how can we handle? support cmux?

# Summary

`install.sh` today forces an iTerm2 install/check on every run, regardless of
whether the user already has a working terminal backend (tmux, optionally
paired with Ghostty as its viewer). This feature makes install-time detection
backend-aware: it checks whether the user already has a supported, usable
terminal setup and only suggests installing something when nothing supported
is available. It also folds in adding `cmux` as a third selectable backend
(alongside `iterm2`/`tmux`), per the already-decided
`docs/design/2026-09-03-cmux-backend-scoping-adr.md` — contingent on that
ADR's own gating spike (`docs/research/2026-09-03-cmux-backend-spike.md`)
being run and passing. This brief does not re-litigate that ADR's design; it
adopts its already-scoped plan into this effort and defines the product-level
detection/fallback behavior around it.

# Problem Statement

Doctor unconditionally checks for and auto-installs iTerm2
(`environment.check_environment`), even for users who already have a fully
working setup via the `tmux` backend (with or without Ghostty as its
viewer). This forces an unnecessary GUI app install on users who never intend
to use iTerm2, and gives no feedback about what *is* already usable.

# Business Goal

Users who have already set themselves up with a supported non-iTerm2 backend
(tmux, optionally with Ghostty) should not be forced through an iTerm2
install just to pass doctor/install. Reducing unnecessary installs lowers
friction and support questions for tmux users, and makes doctor's behavior
match what the tool actually supports.

# User Goal

As a user running `install.sh` or `claudespace doctor`, I want the tool to
recognize a terminal setup I already have (iTerm2, or tmux + a viewer like
Ghostty) and only be prompted to install something if I truly have no
supported option available.

# Scope

1. Doctor/install detection recognizes "already supported and usable"
   states, in addition to the iTerm2 check that exists today:
   - iTerm2 is installed.
   - The `tmux` backend is usable (tmux itself is available via
     `tmux_cli.is_tmux_available()`, and — if a GUI viewer is configured/
     applicable — that viewer, e.g. Ghostty, is installed).
   - The `cmux` backend is usable (cmux app installed and its socket
     reachable) — **only once `CmuxBackend` exists**, i.e. contingent on
     item 7 below having shipped.
2. If at least one supported, usable setup is found, doctor does **not**
   auto-install or prompt to install iTerm2.
3. If no supported, usable setup is found at all, doctor suggests installing
   one (the existing iTerm2 install path is retained as the suggested
   default when nothing else is available), consistent with today's
   non-interactive (`--yes`) behavior.
4. Detection of Ghostty presence (`is_ghostty_installed()`-equivalent) is
   added, following the existing `is_iterm_installed()`
   bundle-ID-then-well-known-path pattern, so tmux+Ghostty can be recognized
   as a usable setup rather than assumed.
5. This applies to both entry points that currently run the iTerm2-only
   check: `install.sh`'s `claudespace doctor --yes --no-launch` call, and the
   real-run path's `_ensure_terminal_launched` → `environment.ensure_environment`
   call (today the latter already special-cases skipping iTerm2 entirely for
   `TmuxBackend`; this brief generalizes the *detection/suggestion* logic
   doctor itself runs so both paths agree on what counts as "already set
   up").
6. `DEFAULT_TERMINAL_BACKEND` (`iterm2`, absent config) is left unchanged.
   This feature only changes what doctor auto-installs/warns about; it does
   not change which backend is selected at runtime for a user with no
   `[terminal] backend` configured.
7. Add `cmux` as a third selectable terminal backend (`KNOWN_TERMINAL_BACKENDS`
   gains `"cmux"`, `get_backend()` dispatches to a `CmuxBackend`), per the
   already-decided plan in
   `docs/design/2026-09-03-cmux-backend-scoping-adr.md`. This is **gated**:
   the ADR's own spike (`docs/research/2026-09-03-cmux-backend-spike.md`)
   must be run and pass (GO or conditional GO) before `CmuxBackend` is
   implemented and before doctor treats cmux as a usable/supported setup. If
   the spike comes back NO-GO, this item does not ship and the feature
   proceeds with iTerm2 + tmux detection only (items 1–6).
8. Add `is_cmux_installed()`-equivalent detection (app present, socket
   reachable), used both by doctor's "usable setup" check (item 1, once
   gated on item 7) and by backend selection.

# Out of Scope

- Re-deciding whether/how `CmuxBackend` should work internally (socket API
  mapping, per-pane state re-homing) — already fully specified in the ADR;
  this brief adopts that plan rather than redesigning it.
- Running the spike itself as part of *planning* — executing it and acting on
  its result is engineering's/implementation's call (per the ADR), not
  something this brief does. This brief defines what ships *if* it passes and
  what happens (nothing new ships) *if* it doesn't.
- Changing `DEFAULT_TERMINAL_BACKEND` or otherwise altering which backend a
  user with no explicit config lands on at runtime — cmux, once added, is
  opt-in via explicit `[terminal] backend = "cmux"` config, same as `tmux`
  today.
- Windows/non-Darwin support (tracked separately in
  `docs/design/2026-09-03-windows-support-psmux-scoping-adr.md`).
- Any change to `install.sh`'s Darwin-only refusal gate.
- Deprecating or replacing the iTerm2 backend — cmux is added alongside it.

# Functional Requirements

1. Given a user has iTerm2 installed and no `[terminal] backend` configured,
   when `install.sh` / `claudespace doctor` runs, then it must not prompt for
   or perform an iTerm2 install (unchanged from today).
2. Given a user does **not** have iTerm2 installed, but has `tmux` available
   and either does not require a GUI viewer or already has the configured
   viewer (default: Ghostty) installed, when doctor runs, then it must
   recognize this as a usable, supported setup and must not auto-install or
   prompt to install iTerm2.
3. Given a user has neither iTerm2 installed nor a usable tmux(+viewer)
   setup, when doctor runs, then it must fall back to today's behavior:
   suggest/auto-install iTerm2 (respecting `--yes`/interactive prompting as
   it does today).
4. Given a user previously had a usable tmux+Ghostty setup and then deletes
   Ghostty (and has no iTerm2), when doctor runs, then it must detect that no
   supported, usable setup remains and must suggest installing one (falling
   back to requirement 3), rather than silently reporting success.
5. Given a user asks doctor to check their setup, when no supported backend
   is usable, then the doctor output must state that no supported terminal
   setup was found before it proceeds to install/suggest one, so the user
   understands why an install is being suggested.
6. Detection must not change `DEFAULT_TERMINAL_BACKEND` or silently alter
   which backend is selected at runtime for a user with no explicit
   `[terminal] backend` config — this feature only changes what doctor
   auto-installs/warns about (per Scope item 6).
7. Given the cmux spike (`docs/research/2026-09-03-cmux-backend-spike.md`)
   has been run and passed (GO or conditional GO), when a user sets
   `[terminal] backend = "cmux"`, then `get_backend()` must return a working
   `CmuxBackend` instead of raising `ValueError`.
8. Given `CmuxBackend` exists and cmux is installed with a reachable socket,
   when doctor runs with no `[terminal] backend` configured, then it must
   recognize cmux as a usable, supported setup and must not auto-install or
   prompt to install iTerm2 (same treatment as requirement 2, extended to
   cmux).
9. Given the spike has not been run, or was run and returned NO-GO, when
   doctor runs, then cmux must **not** be treated as a usable/supported
   setup (no `CmuxBackend` exists to select), and behavior must fall back
   unchanged to requirements 1–6 (iTerm2/tmux detection only).

# Non-functional Requirements

- **Reliability**: Detection must degrade the same way `is_iterm_installed()`
  does today — checking a bundle ID/well-known path, not assuming presence
  from config alone (e.g. a configured `tmux` backend with Ghostty as viewer
  must not be trusted as "usable" without actually probing for tmux and
  Ghostty being present).
- **Usability**: Non-interactive installs (`install.sh` running
  `doctor --yes --no-launch`, no TTY in the general curl-pipe-sh case) must
  continue to work without hanging on a prompt.
- **Consistency**: The same "is a supported setup usable" check must produce
  the same answer whether invoked from `install.sh`'s doctor call or from the
  real-run path's environment check, so a user doesn't get contradictory
  signals between `claudespace doctor` and `claudespace` (no subcommand).

# User Flow

1. User runs `install.sh` (fresh install) or `claudespace doctor` (existing
   install, re-check).
2. Tool checks whether a supported, usable terminal setup already exists
   (iTerm2 installed, or tmux+required-viewer installed).
3. If yes: tool proceeds without prompting for or installing iTerm2 (or any
   other terminal app).
4. If no: tool reports that no supported setup was found and suggests/
   installs iTerm2 (today's fallback), respecting `--yes`/interactive mode as
   it does today.

# Constraints

- Must not change the documented, intentional default backend behavior:
  "Absent file or key defaults to iTerm2 ... never a silent third option."
- `CmuxBackend` implementation and any doctor treatment of cmux as "usable"
  must not begin/ship ahead of the spike being run and passing — that
  sequencing is an explicit, already-made decision in the existing ADR. This
  brief includes cmux in scope but does not waive that gate.
- Must continue to support non-interactive installs (`--yes`).

# Assumptions

- Q: Should "detect what's supported and suggest installing only if nothing
  is available" change the *default* backend selection (`iterm2` absent
  config), or purely change what doctor auto-installs/warns about? -> A:
  Purely change what doctor auto-installs/warns about; `DEFAULT_TERMINAL_BACKEND`
  stays unchanged. Smallest blast radius, and the existing default is
  explicitly documented as an intentional decision the codebase already made
  ("never a silent third option") — this feature is about not forcing an
  unnecessary install, not about changing which backend a user ends up on.
  (decided autonomously)
- Q: When multiple supported backends are usable at once (e.g. user has both
  iTerm2 and a working tmux+Ghostty setup), what should doctor do? -> A:
  Nothing — if *any* supported, usable setup exists, doctor does not
  auto-install or suggest anything. It does not need to pick a "preferred"
  one; that's what `DEFAULT_TERMINAL_BACKEND`/explicit config already do at
  runtime, which this feature does not touch. (decided autonomously)
- Q: Should cmux support be built now, ahead of its own gated spike? -> A:
  Cmux support is included in this brief's scope at the user's explicit
  direction (overriding the earlier autonomous default of excluding it).
  The spike-gate sequencing from the ADR is kept as-is: `CmuxBackend`
  implementation and doctor's treatment of cmux as "usable" both remain
  contingent on the spike being run and passing. This is not a product
  scope question — it's an unresolved technical validation the ADR already
  requires before any cmux code ships — so the gate itself is preserved even
  though the feature now includes cmux. (user-directed scope change,
  2026-09-03)
- Assumes tmux's "usable" bar for this feature is: tmux itself present
  (`tmux_cli.is_tmux_available()`), and if a GUI viewer is required for the
  user's tmux usage, that viewer (default Ghostty) is present too. Exact
  detection mechanics (e.g. how "viewer required" is determined) are an
  engineering decision, not a product one.

# Risks

- A user who currently relies on doctor's forced iTerm2 install as their
  *de facto* way of getting a working terminal (even though they didn't
  choose tmux) could be confused if doctor now stops offering iTerm2 when it
  detects tmux — mitigated by requirement 5 (doctor states what it found and
  why before acting).
- If tmux/Ghostty detection has a false positive (reports "usable" when it
  isn't), a user could end up with no working terminal launch path at
  runtime and no doctor warning to explain why — mitigated by requirement 4
  (re-check on every doctor run, don't cache/assume).
- The cmux spike may return NO-GO (e.g. reads only work on a focused
  surface, or no field survives to carry role/instance identity — see the
  spike's Part A must-pass list). If so, requirements 7–9's cmux work does
  not ship at all, and this feature reduces to iTerm2/tmux detection only.
  Scoping cmux into this same effort means that risk is now shared with the
  detection work rather than isolated to a separate future feature.
- Running the spike itself takes real setup (a macOS 14+ machine with cmux
  installed, ~half a day timeboxed) and is a hard prerequisite this brief
  does not shrink or skip.

# Open Questions

None outstanding — the product questions raised by the original request
(default-backend impact, multi-backend priority, cmux timing) were resolved
under Assumptions per autonomous-mode decision-making. Any disagreement with
those calls is a candidate for revision, not a blocker to proceeding.

# Acceptance Criteria

- Given iTerm2 is installed, when `install.sh` runs, then no iTerm2
  install/prompt occurs (regression check against current behavior).
- Given iTerm2 is not installed but tmux + the configured viewer (Ghostty)
  are both installed, when `claudespace doctor` runs, then no iTerm2
  install/prompt occurs and doctor reports the detected usable setup.
- Given neither iTerm2 nor a usable tmux(+viewer) setup exists, when
  `claudespace doctor --yes --no-launch` runs, then it installs iTerm2
  exactly as it does today, after reporting that no supported setup was
  found.
- Given a user had a usable tmux+Ghostty setup and Ghostty is removed, when
  `claudespace doctor` runs again, then it no longer reports a usable setup
  and falls back to the iTerm2 suggestion/install path.
- Given no `[terminal] backend` is configured, when doctor changes its
  install suggestion behavior per the above, then `load_terminal_backend()`
  still returns `iterm2` (default backend selection is unaffected).
- Given the spike has passed and `CmuxBackend` ships, when a user sets
  `[terminal] backend = "cmux"`, then `claudespace` launches via cmux instead
  of raising `ValueError`.
- Given the spike has passed and cmux is installed with a reachable socket,
  when doctor runs with no backend configured, then it recognizes cmux as a
  usable setup and does not prompt for an iTerm2 install.
- Given the spike has not been run (or returned NO-GO), when doctor or
  `get_backend()` run, then cmux is treated as fully unsupported — no
  `"cmux"` config value is accepted, and doctor never reports it as a usable
  setup.

# Success Criteria

- Users on the `tmux` backend with a working viewer no longer get an
  unrequested iTerm2 install during `install.sh` or `claudespace doctor`.
- Doctor output clearly communicates what supported setup (if any) it found,
  reducing "why is this installing iTerm2 on my machine" confusion/support
  questions.
- No regression in the existing iTerm2-only fallback path for users with no
  supported backend at all.
