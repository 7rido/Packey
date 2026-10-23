"""Confirmation-flow regression tests for the destructive-action guards.

The outage these pin down: storage.prune_confirmations() had been left ending in
a stray `return _Fernet(key)` from an edit. store_confirmation() prunes before
it inserts, so the FIRST confirmation stored after an empty state died with
UnboundLocalError (no `key` was ever bound), and every later one died inside
`_Fernet(a confirmation tuple)`. The whole confirmation flow was dead — the flow
that stands between a user and /remove, /purge or /clear.

Two rules follow, and both are tested here:
  1. prune_confirmations() returns None and never raises, on an empty store and
     on a populated one, and
  2. a stored confirmation round-trips through get_confirmation, while an
     expired one is pruned away and the live ones survive.
"""
import time

import pytest

import storage


@pytest.fixture
def confirmations():
    """storage.pending_confirmations is process-wide; leave it as we found it."""
    storage.pending_confirmations.clear()
    yield storage.pending_confirmations
    storage.pending_confirmations.clear()


def _expire(key):
    """Backdate one entry past CONFIRM_TTL, the way time passing would."""
    user_id, _ = storage.pending_confirmations[key]
    storage.pending_confirmations[key] = (
        user_id, time.monotonic() - storage.CONFIRM_TTL - 1)


def test_store_confirmation_works_on_an_empty_store(confirmations):
    """The crash: the first confirmation stored after a restart went nowhere."""
    assert confirmations == {}
    storage.store_confirmation(("remove", 42, "94001"), 42)      # must not raise
    assert storage.get_confirmation(("remove", 42, "94001")) == 42


def test_prune_returns_none_on_an_empty_store(confirmations):
    """The stray `return _Fernet(key)` hit UnboundLocalError here."""
    assert confirmations == {}
    assert storage.prune_confirmations() is None


def test_prune_returns_none_with_entries_and_drops_only_the_expired(
        confirmations):
    """...and `_Fernet(a confirmation tuple)` here."""
    storage.store_confirmation(("purge", 7, "all"), 7)
    storage.store_confirmation(("clear", 8, "all"), 8)
    _expire(("purge", 7, "all"))

    assert storage.prune_confirmations() is None                # must not raise
    assert ("purge", 7, "all") not in confirmations, "expired entry survived"
    assert ("clear", 8, "all") in confirmations, "a live confirmation was pruned"


def test_get_confirmation_drops_an_expired_entry(confirmations):
    """An expired confirmation must not be honoured, and must not linger."""
    storage.store_confirmation(("remove", 9, "94002"), 9)
    _expire(("remove", 9, "94002"))

    assert storage.get_confirmation(("remove", 9, "94002")) is None
    assert ("remove", 9, "94002") not in confirmations
