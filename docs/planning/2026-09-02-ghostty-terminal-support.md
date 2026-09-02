# Original Request

"how can we support ghostty ?" — user wants claudespace to work with the Ghostty terminal emulator, not just iTerm2.

---

# Summary

Add a second, user-selectable claudespace backend that runs the pipeline inside a **tmux session** rather than driving Ghostty's terminal API natively. iTerm2 remains the default and untouched; the tmux backend is opt-in, primarily positioned as "how you use claudespace in Ghostty," and provides full behavioral parity with the current iTerm2 experience (not a reduced feature set) because tmux gives us the two capabilities Ghostty's own automation API currently lacks. Running unmodified in any tmux-capable terminal is an accepted side effect of this approach, not a separate deliverable.

**Revision note (supersedes the original native-Ghostty framing):** design work on the original "native `GhosttyBackend`" scope surfaced that Ghostty's 1.3 AppleScript API has no screen-read and no per-pane-variable primitives, which would have forced this brief's own parity requirement (Functional Requirements 3, 5, 7 below) to be relaxed to best-effort/degraded behavior. tmux's `capture-pane` and `set -p` close both gaps outright, so this brief now scopes the tmux-backed approach instead — see Assumptions.

---

# Problem Statement

claudespace's entire terminal-automation layer is hard-wired to iTerm2's Python API. Users who prefer or already use Ghostty as their terminal cannot use claudespace at all — there is no backend choice.

---

# Business Goal

Remove a terminal-choice lock-in that currently excludes Ghostty users from claudespace entirely, without destabilizing the tool for existing iTerm2 users — and do it at the lowest ongoing maintenance cost: one backend implementation rather than one hand-written implementation per terminal, each inheriting that terminal's own automation gaps.

---

# User Goal

Run the full claudespace pipeline (workspace build, role handoff, prompt delivery, monitoring) inside Ghostty instead of iTerm2, with no loss of functionality versus the current iTerm2 experience.

---

# Scope

- A user-facing way to select the tmux backend for claudespace (config-based), with iTerm2 remaining the default when nothing is set.
- Full parity, not degraded/best-effort behavior, for every pipeline behavior currently provided by the iTerm2 backend, delivered via tmux:
  - workspace pane/window layout build (as tmux windows/panes)
  - role prompt injection and delivery confirmation (via tmux's screen-read capability)
  - role handoff and pane reveal (conductor → researcher → planner → principal → implementer → reviewer)
  - workspace/role state persistence (run doc path, template name, lazy/auto-handoff flags, etc., via tmux's per-pane variable capability)
  - ad hoc pane messaging (`claudespace-msg`)
  - full-fidelity background session/watchdog monitoring (not crash-detection-only)
- Clear, actionable failure behavior when tmux or the underlying terminal automation needed to launch it is unavailable.
- Positioning the tmux backend as the supported way to run claudespace in Ghostty; opt-in rather than default given it is new and introduces a new dependency.

---

# Out of Scope

- Hand-written native per-terminal backends beyond the existing iTerm2 one — in particular, no native `GhosttyBackend` driving Ghostty's own automation API directly. That path was evaluated and rejected: Ghostty's current API can't support this brief's parity requirement without degrading it (see Assumptions).
- Formally supporting, testing, or documenting terminals other than iTerm2 and Ghostty. That the tmux backend happens to also work in Kitty/Alacritty/WezTerm/Terminal.app/SSH is an accepted side effect of the approach, not a deliverable of this brief — no commitment to test or support those.
- Non-macOS support.
- Automatic migration of an in-progress iTerm2 workspace to the tmux backend, or mixed-backend workspaces (switching backend mid-workspace run).
- Any change to pipeline/role logic itself (conductor, handoff routing, role prompts) beyond routing it through a backend-selectable terminal layer.
- Making the tmux backend the default.

---

# Functional Requirements

1. The user can select the tmux backend as claudespace's terminal backend via configuration; iTerm2 remains the default when no backend is configured.
2. With the tmux backend selected, starting a claudespace workspace builds the full pipeline pane/window layout (conductor + all role panes) as a tmux session, opened inside the user's terminal (Ghostty).
3. Role prompts are injected and their submission is confirmed correctly in tmux-backed panes, equivalent to current iTerm2 behavior — full confirmation, not best-effort.
4. Role handoff (automatic pane reveal/routing between conductor, researcher, planner, principal, implementer, reviewer) functions identically regardless of backend.
5. Workspace and role state persists across a tmux-backed session (run doc path, template name, lazy/auto-handoff flags, and any other state the pipeline currently keeps per pane), with behavior indistinguishable from iTerm2.
6. Ad hoc pane messaging (`claudespace-msg`) works against tmux-backed panes.
7. Background watchdog/session monitoring works against tmux-backed workspaces with full fidelity (equivalent to current iTerm2 content-stall detection, not reduced to crash-detection-only).
8. If tmux is not installed, or the terminal automation needed to launch a tmux session fails, claudespace surfaces a clear, actionable error instead of hanging or silently falling back to a different backend.
9. Existing iTerm2-backed workspaces and behavior are unchanged by the addition of the tmux backend.

---

# Non-functional Requirements

- **Reliability**: backend failures must surface as clear errors and must not corrupt pipeline state or leave a workspace half-built.
- **Usability**: switching between iTerm2 and the tmux backend is a single config change; no per-command flags required to use the selected backend.
- **Compatibility**: macOS only, matching claudespace's current platform scope; requires tmux to be installed and available on the user's `PATH`.

---

# User Flow

1. User has Ghostty and tmux installed, and sets claudespace's configured terminal backend to tmux.
2. User runs a claudespace command that starts a workspace/pipeline (as they do today with iTerm2).
3. claudespace opens Ghostty and builds the pipeline's pane layout as a tmux session inside it, instead of using iTerm2's native panes.
4. The pipeline proceeds through roles exactly as it does today: prompts are delivered, panes reveal on handoff, ad hoc messages land, state persists, and the watchdog monitors sessions — all inside the tmux session.
5. User can revert the config to switch back to iTerm2 at any time; existing iTerm2 behavior is unaffected.

---

# Constraints

- Must not degrade or break the existing iTerm2 experience for current users.
- macOS only, consistent with claudespace's current platform scope.
- Using this backend requires tmux to be installed — a new runtime dependency that doesn't exist for the current iTerm2-only experience.
- The tmux backend puts the user inside tmux's own chrome (prefix keys, status bar, pane borders) rather than the terminal's native pane/tab UI — an explicit, accepted UX trade-off for the parity and reach this approach buys (see Assumptions and Risks).

---

# Assumptions

- Q: Should claudespace support Ghostty now, given Ghostty's own automation API is an explicit preview likely to change in a near-term release? -> A: Yes — but not by driving that preview API natively. (decided autonomously)
- Q: Native per-terminal backend (a hand-written `GhosttyBackend` on Ghostty's AppleScript API), tmux-everywhere (a single `TmuxBackend`), or both? -> A: tmux-everywhere. Ghostty's current automation API has no screen-read and no per-pane-variable primitives; a native backend would be forced to relax this brief's own parity requirement (best-effort prompt confirmation instead of confirmed, crash-detection-only watchdog instead of full monitoring, and an ad hoc file-store instead of real per-pane state). tmux's screen-read and per-pane-variable capabilities close both gaps outright, need less code than the native workaround path, and are the standard, well-tested mechanism for exactly this kind of scripted pane control. "Both" was rejected as carrying the maintenance cost of two backends for a native option that can't actually meet this brief's requirements today. (decided autonomously)
- Q: Given tmux works in any terminal, does this brief become "support any terminal"? -> A: No — goal stays "support Ghostty"; tmux is the mechanism, not a broadened goal. The backend isn't restricted to Ghostty, but no other terminal gets formal support, testing, or documentation commitments (see Out of Scope). (decided autonomously)
- Q: Is full behavioral parity with iTerm2 required for v1, or is a reduced feature set acceptable? -> A: Full parity is required for all pipeline-critical behaviors listed in Scope (layout build, confirmed prompt injection, handoff/reveal, ad hoc messaging, full-fidelity monitoring, state persistence) — this is what ruled out the native-Ghostty path above. (decided autonomously)
- Q: Should the tmux backend become the default? -> A: No — iTerm2 stays the default; the tmux backend is opt-in via config. Reason: smallest blast radius for existing users, and it introduces a new dependency (tmux) that shouldn't be forced on users who haven't asked for it. (decided autonomously)
- Q: What platform/version scope applies? -> A: macOS only, matching current scope; requires a tmux version supporting `capture-pane` and pane-scoped user options (`set -p`), both long-standard tmux features. (decided autonomously)
- Q: Is background/watchdog monitoring parity required, or can it be deferred? -> A: Required in this scope — a workspace without monitoring is a degraded product experience, not an acceptable v1 gap. This is achievable via tmux (unlike the native path). (decided autonomously)

---

# Risks

- Users who chose Ghostty specifically to get a native, multiplexer-free terminal now have to run inside tmux to use claudespace there — the opposite of what "native Ghostty support" may have implied to them. Mitigated by clearly documenting that claudespace-in-Ghostty runs via tmux under the hood, so expectations are set upfront.
- Taking on a tmux dependency is a new failure surface (not installed, version too old, user's existing tmux config/plugins interfering with claudespace's own session) that didn't exist in the iTerm2-only product.
- Because launching a tmux session inside Ghostty still needs *some* minimal terminal automation to open the window in the first place, a residual dependency on Ghostty's own (preview-status) automation surface may remain for that narrow launch step — the extent of this is a design-time question, not resolved by this brief.

---

# Open Questions

None — the strategic fork raised by design (native-per-terminal vs. tmux-everywhere vs. both) and the questions originally raised by research (whether to build Ghostty support given its preview status, and how to handle state persistence) are resolved above under Assumptions and Risks for planning purposes.

---

# Acceptance Criteria

1. Given claudespace is configured to use the tmux backend, when the user starts a workspace, then the full pipeline pane layout (conductor + all role panes) is built as a tmux session inside Ghostty.
2. Given a tmux-backed workspace, when a role prompt is dispatched, then the prompt is delivered and its submission fully confirmed (not best-effort), equivalent to current iTerm2 behavior.
3. Given a tmux-backed workspace, when a role completes and hands off, then the next role's pane is revealed/activated automatically, equivalent to current iTerm2 behavior.
4. Given a tmux-backed workspace, when workspace/role state (run doc, template, flags) is set during the session, then that state is correctly readable later in the same session.
5. Given a tmux-backed workspace, when `claudespace-msg` is used to message a role pane, then the message is delivered to the correct pane.
6. Given a tmux-backed workspace, when the watchdog polls session state, then it correctly reports state with full fidelity (equivalent to current iTerm2 content-stall detection), not merely whether the pane process is alive.
7. Given the tmux backend is selected but tmux is not installed or the session can't be launched, when the user starts a workspace, then claudespace fails with a clear, actionable error rather than hanging or silently using a different backend.
8. Given no backend is configured, when the user starts a workspace, then claudespace uses iTerm2 exactly as it does today.

---

# Success Criteria

- A full claudespace pipeline run (conductor through reviewer) completes entirely inside a Ghostty-hosted tmux session with no functional gaps versus the equivalent iTerm2 run.
- No regressions reported against existing iTerm2 workflows after this ships.
- The user adopts the tmux backend for day-to-day use in Ghostty, indicating it is a genuinely viable alternative rather than a checkbox feature.
