# @author Daniel McCoy Stephenson
"""In-memory fixed-window rate limits.

Keys are client IP addresses (for sign-in and registration) or account ids
(for writes). They live only in this process's memory, for at most one window,
and are never written anywhere: the service stores no IP addresses.

Why the service limits sign-in itself: UserAuth's own limiter (20 requests per
60 s on /login, /register and /token/refresh, keyed on the remote address,
X-Forwarded-For not trusted by default; UserAuth application.properties and
config/RateLimitFilter.java) sees every request from this service as one
client. Without a per-player limit here, one person guessing passwords would
use up that shared bucket and lock every other player out of signing in.
"""

import threading
import time


class Limit(object):
    __slots__ = ("name", "count", "seconds")

    def __init__(self, name, count, seconds):
        self.name = name
        self.count = count
        self.seconds = seconds


# Per client IP.
SIGNIN_PER_IP = Limit("signin-ip", 10, 60)
REGISTER_PER_IP = Limit("register-ip", 5, 3600)
API_WRITES_PER_IP = Limit("writes-ip", 120, 60)
# Per account (the UserAuth username, normalised).
SIGNIN_PER_USERNAME = Limit("signin-username", 10, 900)
API_WRITES_PER_PLAYER = Limit("writes-player", 60, 60)
ACHIEVEMENTS_PER_PLAYER = Limit("achievements-player", 100, 3600)
ACCOUNT_FORMS_PER_PLAYER = Limit("account-player", 20, 3600)
# Per account and game (RFC 0016 §6): a page uploads at most once per 30 s.
SAVES_UPLOADS_PER_GAME = Limit("saves-uploads", 120, 3600)


class RateLimiter(object):
    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._windows = {}
        self._nextSweep = 0.0

    def hit(self, limit, key):
        """Count one request. Returns 0 if it is allowed, otherwise the number
        of seconds until the window resets (for Retry-After)."""
        now = self._clock()
        with self._lock:
            self._sweep(now)
            slot = (limit.name, key)
            start, count, _ = self._windows.get(slot, (now, 0, limit.seconds))
            if now - start >= limit.seconds:
                start, count = now, 0
            if count >= limit.count:
                return max(1, int(start + limit.seconds - now + 0.999))
            self._windows[slot] = (start, count + 1, limit.seconds)
            return 0

    def _sweep(self, now):
        # Drop expired windows once a minute, so no address outlives its own
        # window by more than a minute.
        if now < self._nextSweep:
            return
        self._nextSweep = now + 60
        for slot in [slot for slot, (start, _, seconds) in self._windows.items() if now - start >= seconds]:
            del self._windows[slot]

    def __len__(self):
        return len(self._windows)
