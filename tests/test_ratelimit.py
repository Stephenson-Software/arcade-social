# @author Daniel McCoy Stephenson
from arcade_social.ratelimit import Limit, RateLimiter


class Clock(object):
    now = 1000.0

    def __call__(self):
        return self.now


def test_fixed_window():
    clock = Clock()
    limiter = RateLimiter(clock)
    limit = Limit("t", 3, 60)
    assert [limiter.hit(limit, "a") for _ in range(3)] == [0, 0, 0]
    wait = limiter.hit(limit, "a")
    assert 1 <= wait <= 60
    assert limiter.hit(limit, "b") == 0  # keys are separate
    assert limiter.hit(Limit("other", 1, 60), "a") == 0  # limits are separate
    clock.now += 60
    assert limiter.hit(limit, "a") == 0


def test_an_expired_window_restarts_before_the_sweep_drops_it():
    # A 10 s window ends long before the once-a-minute sweep, so hit() itself
    # must start a fresh window with the full count.
    clock = Clock()
    limiter = RateLimiter(clock)
    limit = Limit("short", 2, 10)
    assert [limiter.hit(limit, "a") for _ in range(2)] == [0, 0]
    clock.now += 9
    assert limiter.hit(limit, "a") == 1
    clock.now += 1
    assert [limiter.hit(limit, "a") for _ in range(2)] == [0, 0]
    assert limiter.hit(limit, "a") == 10


def test_expired_windows_are_forgotten():
    clock = Clock()
    limiter = RateLimiter(clock)
    for index in range(50):
        limiter.hit(Limit("t", 5, 10), "203.0.113.%d" % index)
    assert len(limiter) == 50
    clock.now += 61
    limiter.hit(Limit("t", 5, 10), "fresh")
    assert len(limiter) == 1
