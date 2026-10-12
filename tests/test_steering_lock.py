import pytest

from backend.steering.lock import LockManager, SteeringError


def test_steering_lock_acquire_handoff_and_stale_token_rejection():
    lock = LockManager("room")
    initial = lock.acquire("user-a", "A")
    assert lock.holds(initial.token, "user-a")
    with pytest.raises(SteeringError):
        lock.acquire("user-b", "B")

    transferred = lock.handoff(initial.token, "user-a", "user-b", "B")
    assert not lock.holds(initial.token, "user-a")
    assert lock.holds(transferred.token, "user-b")
    with pytest.raises(SteeringError):
        lock.handoff(initial.token, "user-a", "user-c", "C")

    released = lock.release(transferred.token)
    assert released.holder_id is None
