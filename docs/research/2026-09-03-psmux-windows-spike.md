# Spike: is psmux tmux-CLI-faithful enough to back claudespace on Windows?

Status: **not yet run.** Go/no-go for the ADR
`docs/design/2026-09-03-windows-support-psmux-scoping-adr.md`.

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

## Results (fill in when run)

- psmux version: …
- Install channel: …
- Part A: A0 … A10 …
- Part B: pass N/48; failures + root-cause (psmux vs harness): …
- Verdict: GO / conditional GO / NO-GO — because …
