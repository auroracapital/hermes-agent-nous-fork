# Slack egress guard — trust boundary finding (2026-09-28, read-only proof)

Checkout: `/home/ubuntu/worktrees/slack-egress-guard`, branch `fix/slack-egress-guard`,
HEAD `bffa5f43bdff1e41a4d3a9c835366a5a73a01d20`. No live changes. Slack stays off.

## Question asked

Can `plugins/platforms/slack/egress_guard.py`'s `grant()`/`claim()` split be hardened,
in code alone, so that a caller in the same process cannot manufacture its own approval?

## Answer: no. Proven, not asserted.

`plugins/platforms/slack/probe_trust_boundary.py` is the executed proof (run log
`/home/ubuntu/.hermes/cache/scratch/probe-trust-boundary-20260928.log`, sha256
`12ec8966933ebdef9e6f2779fd1cf6e69cea77d725ca3a5460516558f0f4b154`):

```
EXPLOITABLE self_issued_grant: self-issued grant() call produced a usable claim
EXPLOITABLE db_path_override: raw SQL row via env-overridden DB path produced a usable claim
EXPLOITABLE marker_path_override: attacker-controlled marker path was accepted as the stop marker
TOTAL 3 EXPLOITABLE 3
```

Three independent probes, same OS user (`ubuntu`), same process class as the caller:

1. **Self-issued grant.** Import `grant()` directly and call it, then `claim()` the same
   text. Nothing prevents this — `grant()` is a public function in a public module.
2. **Database path override.** Point `SLACK_EGRESS_DB` at a file the attacker owns and
   write the `grants` row with raw SQL, never touching `grant()` at all.
3. **Stop-marker override.** Point `SLACK_ESTOP_PATH` at a file the attacker owns and
   write `open` themselves.

## Why another commit cannot fix this

Approval and the ability to send share one process and one OS user. Whoever controls
the process controls both the "did Sam approve this" question and the answer. A
function boundary, a class boundary, or a second table in the same SQLite file is
still inside that one trust domain. This is not a bug in `egress_guard.py`; it is what
"same process, same uid" *means*. The existing tests (`test_egress_guard.py`, 15/15
green, log `/home/ubuntu/.hermes/cache/scratch/egress-test-final-20260928.log`, sha256
`e1710bf7937b26dea58f7f30e59600a422d97376d4065d111c275e944110cb7f`) prove the guard
behaves correctly on the *public* API — the grant, the race, the crash-then-block. They
cannot prove the API itself can't be bypassed by its own caller, because that is not a
property code review can grant.

## What would actually separate them (proposal only — not built, not to be built here)

A second owner outside caller-writable authority:

- A small approval service under its own OS user, reachable only via a local Unix
  socket. The guard's `claim()` becomes an RPC to that socket instead of a same-process
  function call.
- The socket peer identity is checked with `SO_PEERCRED`: the service accepts a grant
  request only from its own uid (a human-driven approval flow — e.g. a Telegram button
  Sam or Michelle taps), never from the calling uid.
- The grants database file is owned by that service's user, mode not writable by the
  caller's user; the caller only ever sees the RPC, never the file.
- The stop marker moves the same way: the service, not a file the caller's uid can
  write, is the source of truth for "open" vs everything else.

This is a host-privilege change (new system user, socket service, file ownership,
possibly a systemd unit) — explicitly out of scope for this read-only, isolated-checkout
task. It is recorded here as the smallest viable separation, not executed.

## Route coverage recap (unchanged from prior verification)

Gateway `adapter.py` (`_call_with_block_fallback`, `_react`, `_standalone_post_text`,
`_standalone_upload_file`), the three direct scripts (`bot-claim.py`,
`tg_slack_bridge.py`, `slack-broadcast-test.py`), and the Claude stdio wrapper
(`claude_stdio_wrapper.py`, unknown binary schema → allowlist-only, unbound schema
never guessed) all call `claim()`/the wrapper before any network effect. This part is
sound *given* a trustworthy grant source. The finding above is that "trustworthy grant
source" cannot be built from code alone when caller and approver share a process and a
uid.

## Explicitly not done in this task

No live Slack call or token use. No real network probe. No system user or socket
service created. No deploy, restart, ESTOP lift, outbox drain, cron, or GO. No merge.
Checkout and all evidence left in place for aRQ's independent review.
