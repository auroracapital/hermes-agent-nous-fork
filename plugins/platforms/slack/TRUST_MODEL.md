# Slack egress trust model

The caller and the approver are different operating-system users.

`egress_guard.py` runs inside the agent process. It holds no database and it
cannot record a grant. `grant()` from that process is refused.

`egress_broker.py` runs as its own user. It owns the grants database and the
stop marker, both mode 0700. It listens on a Unix socket and reads the peer
identity of every connection with `SO_PEERCRED`.

- A grant is accepted only from a uid that is not the caller's uid.
- A claim is accepted only from the caller's uid, and only when a matching
  unconsumed grant exists and the stop marker says exactly `open`.
- `SLACK_EGRESS_DB` and `SLACK_ESTOP_PATH` are ignored. The caller cannot
  choose the database or the marker.
- The caller's `approved` flag is ignored.

Default is deny. No broker, a refused peer, a missing grant, a consumed
grant, a forged text or a closed marker all stop the write before any
network call.

## What a rollback does

Removing the broker, or restoring the older guard that trusted a caller-owned
database, does not open the gate. The routes call `claim()` and `claim()`
fails closed when the broker is absent. The older guard's rows live in a file
the caller owns, and the broker never reads that file.

## What this does not change

Nothing here starts the broker, opens Slack, or lifts the stop marker. Those
are live changes and stay out of scope until an independent review of this
exact commit passes.
