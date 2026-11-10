"""A local receipt's compression catch-up (#140060), committed off the owner loop.

``adopt_untracked_compression`` takes the writer lock, up to ``_WRITE_PATIENCE_S`` when contended. On
the owner loop that stalls every profile it serves (streams, controls, admissions), including during
a hot-profile activation. So the async paths that restore a receipt (boot and hot-profile recovery, a
socket's first read) commit the catch-up in a worker first, and the restore that follows only reads.
A restore reached synchronously on the loop hands it to a tracked worker (``schedule_adoption``).
"""
import asyncio
import logging
from functools import partial

from hermes_state_runtime import RuntimeStoreError

logger = logging.getLogger(__name__)


def on_owner_loop():
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _due(authority, sid):
    """*sid*'s stored receipt when a catch-up would commit for it on this owner, else None. Read-only."""
    from gateway.session_local_recovery import owned_receipt
    from hermes_state_local_lineage import adoptable_compression
    try:
        receipt = owned_receipt(authority, sid)[0]
        with authority.db._read_ctx() as conn:
            return receipt if adoptable_compression(conn, receipt) is not None else None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None  # the restore refuses this receipt and logs the check that failed


def _adopt(authority, sid, receipt):
    from hermes_state_local_lineage import adopt_untracked_compression
    adopt_untracked_compression(authority.db, epoch=authority.epoch, session_id=sid, expected=receipt)


async def adopt_before_restore(authority, sid, *, tracked=True):
    """Commit *sid*'s due catch-up in a worker, so the restore that follows only reads.

    ``tracked=False``: an authority still being built, not yet published for retirement to join."""
    receipt = _due(authority, sid)
    if receipt is None:
        return
    write = partial(_adopt, authority, sid, receipt)
    try:
        if tracked:
            from gateway.session_runtime_workers import tracked_write
            await tracked_write(authority, write)
        else:
            await asyncio.to_thread(write)
    except RuntimeStoreError as exc:  # the restore that follows refuses the receipt and logs it
        logger.warning('Local session %s compression catch-up failed: %s', sid, exc.reason)


def _due_many(authority, session_ids):
    """Ids in *session_ids* whose compression catch-up would commit, read on this turn.

    Receipt JSON is one query per chunk. A tip that is not ended ``compression`` cannot have an
    adoptable child (``_CHAIN_STEP_SQL`` requires that), so the per-tip walk runs only for those
    parents. The result is used before the next await.
    """
    from gateway.session_local_recovery import owned_receipt
    from hermes_state_local import local_receipts
    from hermes_state_local_lineage import adoptable_compression
    receipts = local_receipts(authority.db, session_ids)
    candidates = []
    for sid in session_ids:
        receipt = receipts.get(sid)
        if not isinstance(receipt, dict):
            continue
        try:
            tip = receipt['entry']['session_id']
        except (KeyError, TypeError):
            continue
        if isinstance(tip, str) and tip:
            candidates.append((sid, tip))
    if not candidates:
        return []
    compressed = set()
    tips = [tip for _sid, tip in candidates]
    for start in range(0, len(tips), 400):
        chunk = tips[start:start + 400]
        placeholders = ",".join("?" * len(chunk))
        with authority.db._read_ctx(reopen=False) as conn:
            rows = conn.execute(
                f"SELECT id FROM sessions WHERE end_reason='compression' AND id IN ({placeholders})",
                chunk,
            ).fetchall()
        compressed.update(row[0] for row in rows)
    due = []
    for sid, tip in candidates:
        if tip not in compressed:
            continue
        try:
            receipt = owned_receipt(authority, sid, receipt=receipts[sid])[0]
            with authority.db._read_ctx(reopen=False) as conn:
                adoptable = adoptable_compression(conn, receipt)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if adoptable is not None:
            due.append(sid)
    return due


async def adopt_local_compressions(authority):
    """Boot and hot-profile recovery: every receipt's due catch-up, before ``recover_local_sessions``.

    The id list is read in a worker. Each batch's due check runs on this loop and is consumed
    before the next ``sleep(0)``, so a hot-serve of many receipts still answers liveness probes.
    """
    from gateway.session_local_recovery import _recovery_batches, local_receipt_ids
    ids = await asyncio.to_thread(local_receipt_ids, authority)
    chunks = list(_recovery_batches(ids))
    for index, chunk in enumerate(chunks):
        for sid in _due_many(authority, chunk):
            await adopt_before_restore(authority, sid, tracked=False)
        if index + 1 < len(chunks):
            await asyncio.sleep(0)


def schedule_adoption(authority, sid, receipt):
    """A sync restore on the owner loop found *receipt*'s catch-up due: commit it in a tracked worker,
    then publish the session on the loop. One worker per session at a time."""
    from gateway.session_runtime_workers import track_mutation
    pending = getattr(authority, '_local_adoptions', None)
    if pending is None:
        pending = authority._local_adoptions = {}
    if sid in pending and not pending[sid].done():
        return
    logger.info('Local session %s: committing its compression catch-up off the owner loop', sid)

    async def adopt_then_publish():
        try:
            await asyncio.to_thread(_adopt, authority, sid, receipt)
            if not getattr(authority, 'retiring', False):
                from gateway.session_local_recovery import restore_local_session
                restore_local_session(authority, sid)
        except RuntimeStoreError as exc:
            logger.warning('Local session %s paused after its compression catch-up: %s', sid, exc.reason)
    pending[sid] = track_mutation(authority, adopt_then_publish())
