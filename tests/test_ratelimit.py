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


def test_expired_windows_are_forgotten():
    clock = Clock()
    limiter = RateLimiter(clock)
    for index in range(50):
        limiter.hit(Limit("t", 5, 10), "203.0.113.%d" % index)
    assert len(limiter) == 50
    clock.now += 61
    limiter.hit(Limit("t", 5, 10), "fresh")
    assert len(limiter) == 1
