# Hub-local patch: local-session recovery vs the loop watchdog

Production (ai-hub) runs NousResearch/hermes-agent at `81a55437f3`. On that
commit, startup walks every `gateway.local_policy.v1:*` receipt with one
SQLite read per receipt on the gateway event loop. About 14k open receipts
made the loop miss three liveness probes (defaults: 30s interval, 10s
timeout, 3 strikes) and the process exited 75.

`hermes-local-recovery-81a5543.patch` is that fix as one squashed
`git format-patch` against `81a55437f3` (branch
`lil/gateway-local-recovery-81a5543`).

sha256: `a8fdad53b7b7e5d7cae63d964b581ca9e57d1fb91555e0b2c3dc3feb427270ec`

`git apply --check` of this file against a clean `81a55437f3` checkout exits 0.

Watchdog defaults are not changed. Recovery still restores every receipt in
`local_receipt_ids` order. Only that id list is fetched in a worker. Each
batch's receipt and admission rows are re-read on the loop immediately
before apply, with no await between that read and `restore_local_session`.
Apply stays on the loop, 100 sessions at a time, with `await asyncio.sleep(0)`
between batches. Those batch readers pass `reopen=False`. Progress is logged
every 10% of the scan or every 5s, whichever comes first. Startup and
hot-serve await that pass before webhook finalization and native recovery.

Keep a copy of the patch outside the git checkout (for example
`~/.hermes/patches/hermes-local-recovery-81a5543.patch`). `hermes update`
and a pull of upstream replace the working tree, and this `patches/hub/`
directory is not on upstream, so the in-repo copy disappears with the update.

## Is the patch already present?

From the Hermes checkout:

```bash
grep -n "async def recover_local_sessions_async" gateway/session_local_recovery.py
grep -n "recover_local_sessions_async" gateway/run_runtime.py
grep -n "adopt_local_compressions\|recover_local_sessions_async\|bind_owner" gateway/session_authority.py
grep -n "def adopt_local_compressions\|def _due_many\|asyncio.sleep" gateway/session_local_adoption.py
```

No match on `recover_local_sessions_async` means this patch is not applied.

A match is not enough to keep it. Read the hits:

- `recover_gateway_native_sessions` and `serve_profile_runtime` await
  `recover_local_sessions_async`.
- That function fetches only the id list in a worker
  (`asyncio.to_thread(local_receipt_ids)`). Each batch calls
  `_prefetch_recovery_batch` and then `_apply_recovery_batch` with no await
  between them, then `asyncio.sleep(0)` before the next batch. The receipt
  query is `local_receipts` (one query per chunk, `reopen=False`), not
  `local_receipt` once per session.
- `initialize_session_authority` awaits the yielding `adopt_local_compressions`
  and `recover_local_sessions_async` before `bind_owner` and before it
  publishes `runner.session_authority`.
- `adopt_local_compressions` batches `_due` (`_due_many`) and yields with
  `asyncio.sleep(0)` between those batches.

If upstream already does all of that, drop this patch. Do not re-apply it.

If the name exists but startup still calls synchronous `recover_local_sessions`
on the loop, the async function awaits a prefetch before apply, initialize
publishes before the recover pass, or `adopt_local_compressions` still walks
`_due` one receipt at a time without a yield, the upstream change is a
different one. Re-cut; do not assume the names match.

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
2. Re-cut the same behaviour on that commit. Do not put the per-batch receipt
   or admission read back in a worker: that prefetch is the stale-read bug.
   Keep this shape:
   - only `local_receipt_ids` is fetched with `asyncio.to_thread`
   - each batch's receipt and admission rows are read on the loop
     (`reopen=False`) and applied immediately, with no await between the
     read and `restore_local_session`
   - `asyncio.sleep(0)` between batches of 100, in `local_receipt_ids` order
   - progress logged every 10% of the scan or every 5s, whichever comes first
   - `initialize_session_authority` awaits the yielding `adopt_local_compressions`
     (batched `_due`, `asyncio.sleep(0)` between those batches) and the
     recover pass before `bind_owner` and before publish
   - watchdog defaults left alone
3. `git format-patch -1` the new commit, replace the saved patch, and record
   the new sha256.
4. `git apply --check` that new file against a clean checkout of the commit
   you cut it on. It must exit 0.

## When to delete the patch

Drop it when upstream startup no longer blocks the loop on per-receipt
SQLite reads. The checklist above is the one to use: on-loop read-then-apply
per batch, a yielding adopt pass, and publish only after both. Until that is
true on the commit the hub runs, keep the patch.
