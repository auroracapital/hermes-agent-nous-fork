# Hub-local patch: local-session recovery vs the loop watchdog

Production (ai-hub) runs NousResearch/hermes-agent at `81a55437f3`. On that
commit, startup walks every `gateway.local_policy.v1:*` receipt with one
SQLite read per receipt on the gateway event loop. About 14k open receipts
made the loop miss three liveness probes (defaults: 30s interval, 10s
timeout, 3 strikes) and the process exited 75.

`hermes-local-recovery-81a5543.patch` is that fix, cut as
`git format-patch -1` of `403d2133af` whose parent is `81a55437f3`.

sha256: `3d3d6f5eec03d3e136b1aa04d7a13de802e38af196373fed40817346148a96c2`

`git apply --check` of this file against a clean `81a55437f3` checkout exits 0.

Watchdog defaults are not changed. Recovery still restores every receipt in
`local_receipt_ids` order. Reads for each batch run in a worker thread.
`restore_local_session` stays on the loop, 100 sessions at a time, with
`await asyncio.sleep(0)` between batches. Startup and hot-serve await that
pass before webhook finalization and native recovery.

Keep a copy of the patch outside the git checkout (for example
`~/.hermes/patches/hermes-local-recovery-81a5543.patch`). `hermes update`
and a pull of upstream replace the working tree, and this `patches/hub/`
directory is not on upstream, so the in-repo copy disappears with the update.

## Is the patch already present?

From the Hermes checkout:

```bash
grep -n "async def recover_local_sessions_async" gateway/session_local_recovery.py
grep -n "recover_local_sessions_async" gateway/run_runtime.py
```

No match means this patch is not applied.

A match is not enough to keep it. Read the two hits:

- `recover_gateway_native_sessions` and `serve_profile_runtime` await
  `recover_local_sessions_async`.
- That function yields with `asyncio.sleep(0)` between batches of
  `_RECOVERY_BATCH` (100), and the receipt query is `local_receipts` (one
  query per chunk), not `local_receipt` once per session.

If upstream already does both, drop this patch. Do not re-apply it.

If the name exists but startup still calls synchronous `recover_local_sessions`
on the loop, or the async function does not yield between batches, the
upstream change is a different one. Re-cut; do not assume the names match.

## Re-apply after `hermes update` or an upstream pull

Stop the gateway first. From the checkout that update just left you on:

```bash
sha256sum ~/.hermes/patches/hermes-local-recovery-81a5543.patch
git apply --check ~/.hermes/patches/hermes-local-recovery-81a5543.patch
git apply ~/.hermes/patches/hermes-local-recovery-81a5543.patch
```

`git apply --check` must exit 0 before `git apply`. The file is a
format-patch, so a checkout where you commit the result can use `git am`
instead of `git apply`:

```bash
git am --check ~/.hermes/patches/hermes-local-recovery-81a5543.patch
git am ~/.hermes/patches/hermes-local-recovery-81a5543.patch
```

Confirm afterwards with the grep above, then start the gateway.

## When `--check` fails

The patch no longer matches the tree. Do not use `--reject`, `-3`, or a
manual hunk edit on the hub.

1. Note the commit the install is on (`git rev-parse HEAD`).
2. Re-cut the same behaviour on that commit: batched worker-thread reads,
   restore on the loop 100 at a time with `asyncio.sleep(0)`, same progress
   logs, watchdog defaults left alone, same ordering as `local_receipt_ids`.
3. `git format-patch -1` the new commit, replace the saved patch, and record
   the new sha256.
4. `git apply --check` that new file against a clean checkout of the commit
   you cut it on. It must exit 0.

## When to delete the patch

Drop it when upstream startup no longer blocks the loop on per-receipt
SQLite reads. The check in the first section is the one to use: an awaited
batched recovery that yields between batches. Until that is true on the
commit the hub runs, keep the patch.
