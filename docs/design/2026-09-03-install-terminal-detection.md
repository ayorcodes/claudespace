# Implementation Design: backend-aware install/doctor terminal detection

Status: **Accepted** — ready for implementation (Increment 1). Increment 2
(cmux) is gated; see Architecture Decisions AD4 and Implementation Order.

Date: 2026-09-03

# References

- Planning Brief: `docs/planning/2026-09-03-install-terminal-detection.md`
- Technical Brief: `docs/research/2026-09-03-install-terminal-detection.md`
- cmux backend ADR (adopted, not re-litigated):
  `docs/design/2026-09-03-cmux-backend-scoping-adr.md`
- cmux go/no-go spike (gating Increment 2):
  `docs/research/2026-09-03-cmux-backend-spike.md`
- Per-session marker scoping (cmux state re-homing depends on it):
  `docs/design/2026-09-03-per-session-marker-scoping.md`

This design covers *how*; the brief and technical brief cover *what* and
*as-is*. It is not re-summarised here.

# Architecture Decisions

## AD1 — Split doctor's environment check from the iTerm2-specific ensure

`environment.check_environment()` today is called from two contexts with
different meaning:

- **Doctor / install** (`cli.py:429`, backend-agnostic) — "is *any* supported
  setup usable; only install something if not."
- **Real run** (`ensure_environment` → `_ensure_terminal_launched`,
  `cli.py:397`, and watchdog `cli.py:448`) — reached **only for
  `ItermBackend`** (the tmux path is already skipped, `cli.py:393-394`), so it
  means "ensure iTerm2 specifically works," and *must* still install iTerm2
  when absent because the user explicitly chose it.

Making one function detection-based would break the real-run iTerm2 path: a
user configured `backend = "iterm2"` who also happens to have a usable
tmux+viewer would have their iTerm2 install silently skipped and the run would
fail.

**Decision:** add a new `run_doctor_checks(...)` for the backend-agnostic
doctor/install path; leave `check_environment()`/`ensure_environment()`
unchanged as the iTerm2-specific ensure the real-run and watchdog paths keep
using. Doctor is the *only* backend-agnostic caller, so this split is total and
low-risk.

**Rejected:** a `require_iterm: bool` flag on `check_environment`. One function
straddling two responsibilities (detect-any vs ensure-iterm) reads worse and
invites the exact mode-confusion bug above; a named second entry point is
clearer.

## AD2 — A single `detect_usable_backends()` is the shared source of truth

Requirement 5/NFR-Consistency want doctor to *report* what it found, and
install/real-run to agree on "usable." One function returning the list of
usable supported setups serves both: doctor branches on emptiness and prints
the list; the runtime paths don't need it (they already know their backend).

Returning a **list** (not a bool) satisfies requirement 5 ("state what was
found") at no extra cost and keeps the multi-usable case (Assumptions Q2)
trivial: any non-empty list ⇒ don't install.

## AD3 — Generalise the existing bundle-ID detection pattern; don't invent a new one

`is_iterm_installed()` already encodes the house pattern: check well-known
`.app` paths, then broaden via `mdfind` by bundle ID. Ghostty's bundle ID
already lives in the repo (`utils.GHOSTTY_BUNDLE_ID`,
`utils._VIEWER_BUNDLE_IDS`). Extract a private
`_app_installed(bundle_id, app_paths=())` in `environment.py`, reimplement
`is_iterm_installed()` on top of it (behaviour identical), and add
`is_ghostty_installed()` / viewer detection through it. This satisfies the
NFR-Reliability requirement ("degrade the same way `is_iterm_installed` does,
don't trust config alone") by construction.

## AD4 — cmux ships as a separate, gated increment; it does NOT ship in this pass

Per the cmux ADR's explicit sequencing and Planning Brief items 7–9 +
Constraints, `CmuxBackend` and doctor's treatment of cmux as "usable" are
contingent on the spike (`docs/research/2026-09-03-cmux-backend-spike.md`)
being **run and passing (GO / conditional GO)**. The spike's Results section is
empty — it has not been run, and running it requires a physical macOS 14+
machine with cmux installed (a hard prerequisite the brief does not shrink).

**Decision (recorded as an autonomously-resolved gate, see Open Questions):**
Increment 1 (items 1–6: iTerm2 + tmux detection) is designed for immediate
implementation. Increment 2 (items 7–9: cmux) is fully designed here but
**gated** — implementer executes it *only after* the spike is run and returns
GO/conditional-GO, and not before. If the spike returns NO-GO, Increment 2 is
dropped and the feature is complete at Increment 1 (requirement 9). Increment 1
is structured so adding cmux later is a localised addition (one detection
branch + one config entry + one dispatch branch + the backend file), requiring
no rework of Increment 1.

## AD5 — Correct home

Detection/suggestion logic → `claudespace/environment.py` (already owns
`is_iterm_installed`/`install_iterm_via_brew`/`is_brew_available`). `"cmux"`
registry entry → `claudespace/config.py`. `CmuxBackend` → `claudespace/backends/cmux.py`
dispatched from `backends/__init__.py`, per the ADR. `install.sh` stays thin
(no detection logic added there — it just keeps calling `claudespace doctor`).
This matches the Technical Brief's "Correct home" analysis exactly.

# Components

Increment 1:

- `claudespace/environment.py` — new detection + doctor entry point (bulk of
  the change).
- `claudespace/cli.py` — doctor subcommand calls the new entry point.
- `claudespace/utils.py` — read-only reuse of `GHOSTTY_BUNDLE_ID` /
  `_VIEWER_BUNDLE_IDS` (promote the viewer map to a non-underscore accessor if
  environment.py needs it, rather than importing a private name).
- `claudespace/config.py` — read-only reuse of `load_tmux_viewer()`.
- `install.sh` — comment/echo wording only (non-behavioural).

Increment 2 (gated):

- `claudespace/config.py` — `KNOWN_TERMINAL_BACKENDS` gains `"cmux"`; error
  strings updated.
- `claudespace/backends/cmux.py` — `CmuxBackend` (per the ADR; not redesigned
  here).
- `claudespace/backends/__init__.py` — `get_backend()` cmux branch.
- `claudespace/environment.py` — `is_cmux_installed()` + cmux branch in
  `detect_usable_backends()`.

# Data Flow

**Doctor / install (`install.sh` → `claudespace doctor --yes --no-launch`):**

```
cli.main() [command == "doctor"]
  → environment.run_doctor_checks(iterm_was_running, assume_yes=args.yes, launch=args.launch)
      require_macos()
      is_claude_installed()                      # unchanged; warn+fail if absent
      usable = detect_usable_backends()          # ["iterm2"], ["tmux"], ["iterm2","tmux"], (+["cmux"] gated), or []
      if not usable:
          logger.warning("No supported terminal setup found (iTerm2, or tmux + its viewer)…")
          install_iterm_via_brew(assume_yes=...)  # existing fallback; returns False → return False
      else:
          logger.info("Found usable terminal setup(s): %s", ", ".join(usable))
      if is_iterm_installed():                    # true after a fallback install, or if already present
          _ensure_api_enabled(iterm_was_running, launch)   # unchanged
      return ok
  → _check_tmux_persistence()                     # unchanged, informational
```

**Real run (`claudespace`, no subcommand) — unchanged:** `_resolve_backend()`
→ `get_backend()` → `_ensure_terminal_launched()` runs `ensure_environment`
**only for the non-tmux (iTerm2) backend**, which still ensures iTerm2
specifically. No detection is involved because the backend is already known.

`detect_usable_backends()` logic:

```
usable = []
if is_iterm_installed():                    usable.append("iterm2")
if tmux_cli.is_tmux_available():
    viewer = config.load_tmux_viewer()      # default "ghostty"
    if _viewer_installed(viewer):           usable.append("tmux")
# Increment 2 (gated) only:
# if "cmux" in KNOWN_TERMINAL_BACKENDS and is_cmux_installed(): usable.append("cmux")
return usable
```

`_viewer_installed(viewer)`: look up `viewer` in the viewer→bundle-ID map
(`utils._VIEWER_BUNDLE_IDS`); if unknown, return `False` (conservative — an
unknown viewer already makes `launch_viewer` raise, so the tmux backend
wouldn't function anyway). If `viewer == "iterm2"`, this is exactly
`is_iterm_installed()`.

# API Changes

None external (single-package CLI, no API contract). Internal:

- `environment.run_doctor_checks(*, iterm_was_running: bool, assume_yes: bool = False, launch: bool = True) -> bool` (new).
- `environment.detect_usable_backends() -> list[str]` (new).
- `environment.is_ghostty_installed() -> bool` (new).
- `environment._app_installed(bundle_id: str, app_paths: tuple[str, ...] = ()) -> bool` (new, private).
- `environment.is_iterm_installed()` — reimplemented on `_app_installed`; signature and behaviour unchanged.
- `check_environment` / `ensure_environment` — **unchanged.**

# Database Changes

None.

# Validation

- `detect_usable_backends()` trusts no config value as evidence of presence —
  every "usable" verdict is backed by a filesystem/`mdfind`/`which` probe
  (NFR-Reliability). A configured `[terminal.tmux] viewer` is only believed
  once its bundle ID is found installed.
- Unknown viewer string ⇒ tmux treated as not usable (see `_viewer_installed`).

# Error Handling

- `is_claude_installed()` failure stays as today (error + `ok=False`).
- Fallback iTerm2 install reuses `install_iterm_via_brew`, which already
  handles: Homebrew absent (clear error, `False`), no TTY without `--yes`
  (clear error, `False`), brew failure (`False`). `run_doctor_checks` returns
  `False` on install failure exactly as `check_environment` does today.
- No new subprocess failure modes: all probes are read-only (`os.path`,
  `mdfind`, `shutil.which`) and already swallow non-zero exit via
  `stderr=DEVNULL`/`returncode` checks.

# Security Considerations

- No new privileged operations. `mdfind`/`which`/path stat are read-only.
- (Increment 2) `is_cmux_installed()` must honour cmux's documented socket
  safety: `$CMUX_SOCKET_PATH` (default `/tmp/cmux.sock`), mode `0600`,
  owner-checked — do not treat a socket owned by another user as usable
  (spike A0). A world-writable `/tmp` socket is otherwise a spoofing surface.

# Performance Considerations

- Detection runs once per `doctor`/install invocation and is negligible: at
  most two `os.path.isdir` stats + one `mdfind` per candidate app, plus
  `shutil.which("tmux")`. No caching needed and none added (requirement 4
  wants a fresh probe every run, not a cached verdict). No collection loads,
  no N+1, no DB access.

# Compatibility

- **Backward compatible.** `DEFAULT_TERMINAL_BACKEND` and
  `load_terminal_backend()` are untouched (requirement 6 / AC5) — runtime
  backend selection is unaffected. Only *what doctor auto-installs/warns about*
  changes.
- The iTerm2-installed user path is unchanged (AC1): detection returns
  `["iterm2"]`, no install, API still enabled.
- No deprecation. cmux is added *alongside* iTerm2/tmux (Increment 2), never
  replacing them.
- (Increment 2) Adding `"cmux"` to `KNOWN_TERMINAL_BACKENDS` is purely
  additive; existing `iterm2`/`tmux` configs are unaffected. Until Increment 2
  ships, a `backend = "cmux"` config still raises the existing `ValueError`
  (requirement 9 / AC-last).

# Edge Cases

- iTerm2 + tmux+ghostty both present → `["iterm2","tmux"]`; no install; iTerm2
  API enabled (default backend is still iterm2). (Assumptions Q2)
- tmux present, viewer configured as `iterm2`, iTerm2 present → tmux usable via
  its iTerm2 viewer; also `"iterm2"` usable.
- tmux present, ghostty removed, no iTerm2 → tmux **not** usable → `[]` →
  fallback install, after the "no supported setup found" message. (AC4)
- Non-interactive, usable setup found → **no prompt at all** (strictly better
  than today's forced check). (NFR-Usability)
- Non-interactive, nothing usable, no `--yes`, no TTY → existing clear error
  from `install_iterm_via_brew`, `run_doctor_checks` returns `False`.
- iTerm2 present but on a custom prefs folder → `_ensure_api_enabled` emits its
  existing guidance; unchanged.
- (Increment 2) cmux socket exists but app absent (stale socket), or socket
  owned by another user → `is_cmux_installed()` returns `False`.

# Tests Required

There is **no `tests/test_environment.py` today** — add one. Monkeypatch the
probe helpers (`os.path.isdir`, the `subprocess.run` used by `mdfind`,
`shutil.which`) rather than the real filesystem.

Unit — `detect_usable_backends()`:
- iTerm2 only present → `["iterm2"]`.
- tmux + ghostty present, no iTerm2 → `["tmux"]`.
- tmux present, ghostty absent, no iTerm2 → `[]`.
- both present → contains both.
- viewer configured `iterm2`, iTerm2 present, tmux present → `"tmux"` usable.
- unknown/garbage viewer → tmux excluded.

Unit — `run_doctor_checks()` (monkeypatch `detect_usable_backends`,
`install_iterm_via_brew`, `is_iterm_installed`, `_ensure_api_enabled`):
- usable non-empty → `install_iterm_via_brew` **not** called; found message
  logged.
- usable empty → `install_iterm_via_brew` called with `assume_yes`; "no
  supported setup found" logged before it.
- usable empty + install fails → returns `False`.
- iTerm2 present → `_ensure_api_enabled` called; absent → not called.
- claude missing → returns `False`.

Unit — `is_ghostty_installed()` / `_app_installed()`: path hit, `mdfind` hit,
neither → correct bool. Confirm `is_iterm_installed()` behaviour is byte-for-
byte preserved after refactor (keep/adapt any existing coverage).

Integration — `cli.py` doctor subcommand: invoking `doctor` calls
`run_doctor_checks` (not `check_environment`) and still calls
`_check_tmux_persistence`; exit code mirrors its bool.

(Increment 2, gated) `is_cmux_installed()` app+socket+owner matrix; `"cmux"`
accepted by `load_terminal_backend`/`get_backend`; cmux added to
`detect_usable_backends` when installed. Plus the spike's own `CmuxBackend`
test suite per the ADR.

# Verification

```
uv run pytest -q
uv run ruff check claudespace tests    # if ruff is configured; otherwise skip
uv run pytest -q tests/test_environment.py tests/test_cli.py
```

Manual smoke (macOS): with iTerm2 installed → `claudespace doctor` performs no
install; temporarily rename `/Applications/iTerm.app` and (if present) Ghostty
→ `claudespace doctor` reports "no supported setup found" and offers/install
iTerm2.

# Implementation Order

**Increment 1 (ships now):**

1. `environment.py`: add `_app_installed(bundle_id, app_paths=())`; reimplement
   `is_iterm_installed()` on it (no behaviour change); add
   `is_ghostty_installed()`.
2. `environment.py`: add `_viewer_installed(viewer)` (using
   `utils._VIEWER_BUNDLE_IDS` / a promoted accessor) and
   `detect_usable_backends()` (iTerm2 + tmux branches only).
3. `environment.py`: add `run_doctor_checks(*, iterm_was_running, assume_yes,
   launch)` per Data Flow. Leave `check_environment`/`ensure_environment`
   untouched.
4. `cli.py`: doctor subcommand (`cli.py:428-437`) calls `run_doctor_checks`
   instead of `check_environment`; keep `_check_tmux_persistence()` and the
   exit-code logic.
5. `install.sh`: update the iTerm2-centric comment/echo wording around
   lines 6-7, 28-30, 149-155 to "checks for a supported terminal setup"
   framing (non-behavioural; the `doctor` call itself is unchanged).
6. Add `tests/test_environment.py` and the doctor integration test; run
   Verification.

**Increment 2 (gated on the cmux spike — do NOT start until it returns GO /
conditional GO):**

7. Run the spike (`docs/research/2026-09-03-cmux-backend-spike.md`) on a real
   macOS 14+ cmux install; record results + verdict. If NO-GO, stop —
   Increment 1 is the finished feature (requirement 9).
8. If GO: implement `backends/cmux.py` (`CmuxBackend`) per the ADR; add cmux
   dispatch in `backends/__init__.py`; add `"cmux"` to
   `KNOWN_TERMINAL_BACKENDS` and update the two `ValueError` strings.
9. `environment.py`: add `is_cmux_installed()` (app + `0600`/owner-checked
   socket) and the gated cmux branch in `detect_usable_backends()`.
10. Tests per the ADR + the cmux rows above; re-run Verification.

# Open Questions

- Q: The cmux spike is unrun and running it needs a physical macOS 14+ cmux
  install that no one in this pipeline has performed. Should Increment 2 ship
  now? → A: No. Increment 1 (iTerm2 + tmux detection) ships now and is complete
  and useful on its own; Increment 2 stays gated exactly as the ADR and brief
  require, and is executed only if/when the spike is run and passes. Designed
  so it slots in without reworking Increment 1. (decided autonomously —
  auditable/reversible: if someone runs the spike and it passes, proceed with
  steps 7–10.)

No engineering uncertainty remains for Increment 1.
