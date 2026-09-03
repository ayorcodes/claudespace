# Note: cmux socket auth blocks the spike CLI probes

Handoff note for implementer — not a full Technical Brief. Local machine
qualifies for the cmux spike (macOS 26.5.2, `brew install --cask cmux`
succeeded, app launched, socket exists at
`~/.local/state/cmux/cmux.sock`, mode 0600, owned by the user).

## What's blocking

Running the spike's CLI probes (`docs/research/2026-09-03-cmux-backend-spike.md`,
Part A) requires the `cmux` CLI to reach the socket. It currently refuses:

```
$ cmux ping
Error: ERROR: Access denied - only processes started inside cmux can connect
```

This is a real, previously-undocumented finding for the spike's A0 (auth
model): the socket is not just 0600/owner-gated — cmux also enforces a
`automation.socketControlMode` setting (schema:
https://raw.githubusercontent.com/manaflow-ai/cmux/main/web/data/cmux.schema.json,
key `automation.socketControlMode`, enum `off|cmuxOnly|automation|password|
allowAll|openAccess|fullOpenAccess|notifications|full`, default `cmuxOnly`).
Under `cmuxOnly`, only processes spawned inside a cmux pane can hit the
socket — a plain external shell (this session's) cannot, regardless of file
permissions.

## The one obvious fix

Add to `~/Users/ayorcodes/.config/cmux/cmux.json` (JSONC, right after
`"schemaVersion": 1,`):

```json
"automation": {
  "socketControlMode": "automation"
},
```

then `cmux reload-config`. A timestamped backup of the pre-edit file exists
at `~/.config/cmux/cmux.json.bak.<timestamp>` (created before this note).

This is outside the repo and outside `researcher`'s write scope (local user
config, not project code), so it needs to be applied by implementer (or by
the user directly) rather than by this role.

## After the config change

Once `cmux ping` succeeds, resume the spike itself
(`docs/research/2026-09-03-cmux-backend-spike.md`, Part A, A0 onward) — that
is genuine researcher/investigation work and should route back to
researcher, not stay with implementer.
