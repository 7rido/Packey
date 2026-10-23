"""The per-chat add locks (storage.get_chat_lock) must serialize AND stay bounded.

Two properties, and the second is only safe because of how it is implemented:

  1. two adds for the same chat never overlap — the lock exists so the
     check → network lookup → insert sequence is atomic (a second caller that
     interleaves across the await passes the package-limit check too, so the cap
     is exceeded and a duplicate 17TRACK registration is burned);
  2. the lock table does not grow forever. It is keyed by chat id, and every
     chat that ever added a package used to leave one behind with nothing to
     remove it.

The trap these tests pin down: bounding the table by evicting an entry while it
is held (or while a coroutine is queued on it) hands the next caller a SECOND
lock for the same chat, and property 1 is gone. asyncio.Lock.release() clears
its flag BEFORE waking the waiter, so a `locked()` check is not enough — the
implementation counts users before the acquire instead. If someone later
"simplifies" _ChatLock away, the waiter test below is what fails.
"""
import asyncio

import storage


def test_locks_are_reused_per_chat_and_are_reentrant_per_holder():
    storage.chat_locks.clear()

    async def scenario():
        # Inside a running loop on purpose: Python 3.9 binds an asyncio.Lock to
        # the loop that created it, and the same caveat the add-race test
        # documents applies here — a lock made outside a loop (or in another
        # one) cannot be used by the loop under test.
        first = storage.get_chat_lock(42)
        assert storage.get_chat_lock(42) is first
        assert storage.get_chat_lock(43) is not first

    asyncio.run(scenario())


def test_the_table_stays_bounded_across_many_chats():
    storage.chat_locks.clear()

    async def scenario():
        for chat_id in range(1000, 1600):
            storage.get_chat_lock(chat_id)

    asyncio.run(scenario())
    assert len(storage.chat_locks) <= storage._CHAT_LOCK_CAP, \
        "a chat that added once must not leave a lock behind forever"


def test_a_lock_in_use_is_never_evicted():
    """The regression that matters: same chat, same lock, held across the churn."""
    storage.chat_locks.clear()

    async def scenario():
        held = storage.get_chat_lock(7)
        async with held:
            # Churn the table far past the cap from inside the critical section.
            for chat_id in range(2000, 2000 + storage._CHAT_LOCK_CAP + 50):
                storage.get_chat_lock(chat_id)
            # The held lock must be the SAME object: a second one would let a
            # second coroutine into the atomic section (property 1, broken).
            return storage.get_chat_lock(7) is held

    assert asyncio.run(scenario()) is True


def test_a_lock_with_a_queued_waiter_is_never_evicted():
    """The window a `locked()` check gets wrong.

    release() clears the flag, then wakes the waiter — so there is an instant
    where the lock reads as unlocked while a coroutine still holds a reference
    to it and is about to acquire. Counting users before the acquire is what
    keeps that waiter's lock out of reach of the evictor.
    """
    storage.chat_locks.clear()

    async def scenario():
        lock = storage.get_chat_lock(9)
        acquired = []

        async def waiter():
            async with lock:
                acquired.append(True)

        # Enter through the wrapper, so the holder is counted: acquiring the
        # inner lock directly would leave _users at 0 and test nothing.
        await lock.__aenter__()
        task = asyncio.create_task(waiter())
        await asyncio.sleep(0)              # let waiter() reach the acquire
        assert lock._users == 2, "holder + queued waiter must both be counted"
        # Churn hard while the waiter is queued.
        for chat_id in range(3000, 3000 + storage._CHAT_LOCK_CAP + 50):
            storage.get_chat_lock(chat_id)
        assert storage.get_chat_lock(9) is lock, \
            "the waiter's lock was evicted from under it"
        await lock.__aexit__(None, None, None)
        await task
        return acquired == [True]

    assert asyncio.run(scenario()) is True


def test_a_released_lock_becomes_idle_and_reusable():
    storage.chat_locks.clear()

    async def scenario():
        lock = storage.get_chat_lock(11)
        assert lock.idle is True
        async with lock:
            assert lock.idle is False
        assert lock.idle is True, "the user counter must decrement on exit"

    asyncio.run(scenario())


def test_the_cap_never_drops_a_lock_below_capacity_needlessly():
    """Idle entries are evicted to make room, not held ones — the table may grow
    past the cap only while locks are genuinely in use."""
    storage.chat_locks.clear()

    async def scenario():
        # Hold one lock, fill the rest with idle entries, then ask for one more:
        # room must come from an idle entry, and the held one must survive.
        held = storage.get_chat_lock(0)
        async with held:
            for chat_id in range(1, storage._CHAT_LOCK_CAP):
                storage.get_chat_lock(chat_id)
            assert len(storage.chat_locks) == storage._CHAT_LOCK_CAP
            storage.get_chat_lock(999999)
            assert len(storage.chat_locks) <= storage._CHAT_LOCK_CAP
            assert 0 in storage.chat_locks, "the held lock was evicted"

    asyncio.run(scenario())
