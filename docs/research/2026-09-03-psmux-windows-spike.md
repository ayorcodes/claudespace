# Spike: is psmux tmux-CLI-faithful enough to back claudespace on Windows?

Status: **RUN 2026-09-05 — verdict NO-GO** (on the ADR's binary-swap bet;
see "A path the ADR did not consider" below). Go/no-go for the ADR
`docs/design/2026-09-03-windows-support-psmux-scoping-adr.md`.
Executed by `.github/workflows/psmux-spike.yml` on a `windows-latest`
runner against psmux `v3.3.8` (pinned release zip). Per-probe raw output:
`docs/research/2026-09-05-psmux-windows-spike-results.md`.

Purpose: convert psmux's *self-reported* tmux compatibility into evidence,
against the exact primitives `backends/tmux_cli.py` uses — before writing any
Windows code. Run on a **Windows 10/11** box. Timebox ~half a day.

## Setup

```powershell
winget install psmux            # or: scoop install psmux / choco install psmux
psmux -V                        # record exact output verbatim (see A0)
$SOCK = "csspike"               # dedicated socket, mirrors -L claudespace
```

Everything below uses `-L $SOCK` so the spike never touches a real session.
Tear down at the end: `psmux -L $SOCK kill-server`.

Notation: **[MUST]** = a failure here is a no-go for the "binary swap" bet.
**[WANT]** = degrades gracefully or has a known workaround.

---

## Part A — direct CLI probes (no Python)

Each item lists the `tmux_cli.py` caller it stands in for, the command to run,
and the expected result. Tick the box only on an exact match.

- [ ] **A0 — version string parses.** `tmux_cli.version()` runs `<bin> -V`;
  `parse_version` expects `tmux X.Y[letter]` and enforces `MIN_TMUX_VERSION =
  (3,0)`. Run `psmux -V`. **[MUST]** output yields a `(major,minor)` ≥ (3,0)
  under `parse_version`, or we learn the version-gate needs a psmux branch.
  Record the raw string.

- [ ] **A1 — detached server, no client.** (`new_session`, `has_session`)
  ```powershell
  psmux -L $SOCK new-session -d -s s1 -c $PWD
  psmux -L $SOCK has-session -t s1     # exit 0
  psmux -L $SOCK list-panes -t s1 -F "#{pane_id}"   # prints %0-style id
  ```
  **[MUST]** session exists and is inspectable with **no terminal attached**.

- [ ] **A2 — capture-pane while detached.** (`capture_pane`, the crux)
  ```powershell
  psmux -L $SOCK send-keys -t s1 -l -- "echo spike-marker-123"
  psmux -L $SOCK send-keys -t s1 Enter
  Start-Sleep -Milliseconds 400
  psmux -L $SOCK capture-pane -p -J -t s1     # must contain spike-marker-123
  ```
  **[MUST]** the text comes back with **no client attached**. This is the
  exact thing zellij cannot do (#4508). If it only works while a viewer is
  attached, that is a no-go for the current watchdog/readiness model.

- [ ] **A3 — `-J` joins a soft-wrapped line.** (`capture_pane` uses `-J` so a
  wrapped prompt matches as one logical line)
  ```powershell
  # 40-col pane, print a >40-char unbroken string, capture with -J
  psmux -L $SOCK new-window -t s1 -n narrow
  psmux -L $SOCK resize-window -t s1:narrow -x 40 -y 10   # or split narrow
  psmux -L $SOCK send-keys -t s1:narrow -l -- "printf 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789XYZ-END'"
  psmux -L $SOCK send-keys -t s1:narrow Enter
  psmux -L $SOCK capture-pane -p -J -t s1:narrow   # ...XYZ-END on ONE line, no mid-word break
  ```
  **[WANT]** join works; if not, readiness/submit matching on long prompts is
  unreliable and needs a workaround.

- [ ] **A4 — per-pane user options round-trip.** (`set_pane_option`,
  `show_pane_option` — the `@cs_*` state model)
  ```powershell
  psmux -L $SOCK set-option -p -t s1 @cs_role researcher
  psmux -L $SOCK show-options -p -v -t s1 @cs_role      # -> researcher
  ```
  **[MUST]** arbitrary `@`-prefixed pane option stores and reads back. This is
  what zellij/wezterm lack; the entire identity model depends on it.

- [ ] **A5 — `@cs_*` interpolates in `list-panes -F` across the server.**
  (`list_panes_all` reads `@cs_workspace/@cs_role/@cs_instance/...` via `-F`)
  ```powershell
  psmux -L $SOCK set-option -p -t s1 @cs_workspace "/some/marker"
  psmux -L $SOCK list-panes -a -F "#{pane_id}#{__US__}#{@cs_workspace}#{__US__}#{@cs_role}"
  ```
  (Use the real `\x1f` unit separator the code splits on.) **[MUST]** custom
  options appear in `-F` output for **`-a` (all sessions on the socket)**, not
  just the current session. If `-a` or `@`-in-`-F` is unsupported, pane
  discovery breaks.

- [ ] **A6 — `send-keys -l --` literal, leading dash safe.**
  (`send_keys_literal`)
  ```powershell
  psmux -L $SOCK send-keys -t s1 -l -- "-not-a-flag typed literally"
  psmux -L $SOCK capture-pane -p -J -t s1   # shows the literal text
  ```
  **[MUST]** `-l` types verbatim and `--` guards a leading `-`.

- [ ] **A7 — named paste buffer, bracketed paste.** (`send_text_paste`, the
  large-prompt fix: `set-buffer -b … -- <text>` then `paste-buffer -d -p -b …`)
  ```powershell
  $big = "HEAD-" + ("x" * 3000) + "-TAIL"
  psmux -L $SOCK set-buffer -b csb -- $big
  psmux -L $SOCK show-buffer -b csb         # returns all ~3010 chars, HEAD..TAIL
  psmux -L $SOCK paste-buffer -d -p -b csb -t s1   # -p bracketed, -d frees buffer
  ```
  **[MUST]** buffer round-trips a >2.5 KB payload byte-intact and
  `paste-buffer -p -d` is accepted. (If unsupported, the handoff-truncation
  fix has no Windows equivalent and we'd regress to the send-keys burst bug.)

- [ ] **A8 — pane geometry + border title.** (`pane_dims` via
  `display-message -p #{pane_width}x#{pane_height}`; `pane_border_title` via
  `select-pane -T` + `pane-border-status/format`)
  ```powershell
  psmux -L $SOCK display-message -p -t s1 "#{pane_width}x#{pane_height}"   # e.g. 80x24
  psmux -L $SOCK select-pane -t s1 -T researcher
  ```
  **[WANT]** dims report real numbers (largest-sibling split sizing);
  border-title is cosmetic and may no-op.

- [ ] **A9 — structure ops.** (`split_window`, `new_window`, `select_pane`,
  `select_window`, `kill_session`) Run each once against `s1`; confirm no
  error and expected pane/window count via `list-panes -a -F "#{pane_id}"`.
  **[MUST]** split/new-window/kill work; **[WANT]** select-* work.

- [ ] **A10 — socket isolation.** Confirm `-L $SOCK` sessions are invisible to
  a bare `psmux list-sessions` (default socket). **[MUST]** namespacing holds
  (mirrors AD8's dedicated socket).

---

## Part B — run the real conformance suite against psmux

The 48 headless tests in `tests/test_tmux_cli.py` and
`tests/test_tmux_backend.py` already encode this contract. Point them at
psmux:

- [ ] **B1 — make `tmux` resolve to psmux.** Either psmux's own `tmux` alias
  is on PATH, or put a `tmux.bat`→`psmux` shim ahead of anything else. Confirm
  `tmux -V` == psmux. (No code change: `tmux_cli.run` execs the literal name
  `tmux`.)
- [ ] **B2 — Python + deps install on Windows.** `pip install -e .` (or uv).
  Note: `themes.py` imports `iterm2` at module scope; if that import fails to
  install on Windows it blocks even the tmux path — record it as a required
  prerequisite fix (it's already in the Linux-native prerequisite list).
- [ ] **B3 — run the suites.**
  ```powershell
  python -m pytest tests/test_tmux_cli.py tests/test_tmux_backend.py -q
  ```
  Record pass/fail **per test**. Expected friction unrelated to psmux
  fidelity: `TMUX_TMPDIR`/socket-path handling and the `FAKE_CLAUDE` shell
  snippet (`printf … while IFS= read`) assume a POSIX shell — some
  `test_tmux_backend.py` cases may need a bash-on-Windows or a psmux-native
  fake. Separate *"psmux can't do X"* from *"the test harness assumed POSIX"*
  in the notes.

- [ ] **B4 — viewer probe (informational, not backend).** Confirm a terminal
  can attach: `wt.exe psmux -L $SOCK attach -t s1` (Windows Terminal), and
  that `launch_viewer` would need a Windows branch (it's macOS `open -b`
  today). Out of scope for go/no-go, in scope for the follow-on work.

---

## Scoring & decision gate

**GO (pursue the binary-swap path in the ADR)** requires **every [MUST]**:
A0, A1, A2, A4, A5, A6, A7, A9(split/new-window/kill), A10 — and Part B
failures all attributable to POSIX-shell/test-harness assumptions, not psmux
command gaps.

**Conditional GO** if all [MUST] pass but some [WANT] (A3 `-J`, A8 dims) fail:
proceed, and log each as a tracked Windows-specific workaround.

**NO-GO (fall back to WSL2 + tmux + Windows Terminal)** if any of these fail:
- **A2** — no detached `capture-pane` ⇒ the headless read model is dead on
  psmux (same wall zellij hit).
- **A4/A5** — no `@cs_*` per-pane options / not in `-F -a` ⇒ the identity
  model needs a rewrite, which means psmux is *not* a swap and the core bet
  failed (kick to a separate backend ADR, or WSL).
- **A7** — no `paste-buffer` ⇒ reintroduces the large-handoff truncation bug
  with no fix path.

Record raw command output for every box (especially A2/A4/A5/A7) in a results
section appended below, plus the exact `psmux -V` and install channel, so the
go/no-go is reproducible and not a vibe.

## Results (run 2026-09-05)

- psmux version: **3.3.8** (`66cf613`, 2026-08-18). Note `psmux -V` prints
  **two** lines — `tmux 3.3.8` then `psmux 3.3.8 (...)` — where tmux prints
  one. `parse_version` tolerates it and yields `(3, 3)`, but it is parsing a
  string it was never designed for; treat as fragile.
- Install channel: pinned GitHub release zip, `windows-x64`, via
  `.github/workflows/psmux-spike.yml` on `windows-latest`.

### Part A — 8/11 pass, 2 MUST fail

| Probe | Level | Result | Note |
| --- | --- | --- | --- |
| A0 version parses | MUST | PASS | two-line output, see above |
| A1 detached server | MUST | PASS | pane id `%1` |
| **A2 detached capture-pane** | MUST | **PASS** | the zellij wall — psmux clears it |
| A3 `-J` join | WANT | **FAIL** | `-J` accepted but does **not** join; the token came back split across two lines |
| **A4 pane `@cs_*` options** | MUST | **FAIL** | `psmux: pane-scoped option '@cs_role' is not supported (supported: remain-on-exit)` |
| **A5 `@cs_*` in `-F -a`** | MUST | **FAIL** | consequence of A4 — rows come back `%1<US><US>` |
| A6 `send-keys -l --` | MUST | PASS | leading dash typed literally |
| A7 paste buffer | MUST | PASS | 3010 chars byte-intact, `paste-buffer -d -p` rc=0 |
| A8 geometry | WANT | PASS | `120x30`; `select-pane -T` rc=0 |
| A9 structure ops | MUST | PASS | split/new-window/select-*/kill all rc=0 |
| A10 socket isolation | MUST | PASS | `-L` namespace invisible to the default socket |

### Part B — 18 passed, 17 failed, one root cause

Every one of the 17 failures is
`TmuxCommandError: psmux: pane-scoped option '@cs_workspace' is not supported`.
**None** are attributable to the POSIX-shell/`TMUX_TMPDIR` friction the spike
anticipated — the classification the gate demanded lands squarely on *psmux
command gap*. `pip install -e .` on Windows succeeded, and
`import claudespace.themes` worked: `iterm2` is a pure `py3-none-any` wheel
(protobuf + websockets; pyobjc only under the `full` extra), so B2 is **not**
the blocker the spike expected.

### Verdict: **NO-GO** on the binary-swap bet

The gate names A4/A5 explicitly: *"no `@cs_*` per-pane options / not in `-F -a`
⇒ the identity model needs a rewrite, which means psmux is **not** a swap and
the core bet failed."* That is exactly what happened, and the failure is
psmux's own explicit error, not an inference.

Note what this cost: psmux's docs advertise `set-option`'s flags as
`guaqopswUt:`, which does include `p`. The **flag parses**; the *option
namespace* behind it is restricted to `remain-on-exit`. Vendor documentation
was accurate about syntax and silent about semantics — the precise reason the
ADR insisted on a spike rather than trusting the compatibility table.

What psmux **does** deliver is not nothing: detached `capture-pane` (A2) is the
wall zellij could not clear, and the paste-buffer path (A7) means the
large-handoff truncation fix has a working Windows equivalent.

### A path the ADR did not consider

The ADR's fallback is "document WSL2 + tmux, close native Windows". But
claudespace **already ships a backend with no per-pane key/value store**:
`backends/cmux.py` carries identity on pane tab titles (`cs:<instance>:<role>`)
and file-homes the remaining mutable state under the session marker directory.
A8 confirms `select-pane -T` works on psmux, so that same model is available
here.

That makes native Windows plausible again — but as a **cmux-style backend
variant, not a binary swap**, which is the outcome the ADR routes to "a
*separate* ADR ... it would mean psmux is not CLI-faithful and the core bet
failed". Decide that there, not here.

Untested and worth one cheap probe before that ADR: whether psmux supports
`@`-prefixed user options at **server or session** scope. `set -g @theme` works
in its own docs. If it does, per-pane state could be homed under synthesised
keys (`@cs_%1_role`) far more cheaply than file-homing.
