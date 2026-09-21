from __future__ import annotations

import unittest

from src.core.rate_limit import SlidingWindowLimiter


class SlidingWindowLimiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocks_until_window_expires(self) -> None:
        now = 100.0
        limiter = SlidingWindowLimiter(2, 10, clock=lambda: now)

        self.assertTrue(await limiter.allow("user"))
        self.assertTrue(await limiter.allow("user"))
        self.assertFalse(await limiter.allow("user"))

        now = 111.0
        self.assertTrue(await limiter.allow("user"))

    async def test_keys_are_independent(self) -> None:
        limiter = SlidingWindowLimiter(1, 60)

        self.assertTrue(await limiter.allow("one"))
        self.assertFalse(await limiter.allow("one"))
        self.assertTrue(await limiter.allow("two"))

    async def test_distinct_active_key_flood_is_hard_bounded(self) -> None:
        now = 100.0
        limiter = SlidingWindowLimiter(1, 60, max_keys=3, clock=lambda: now)

        for index in range(20):
            now += 1
            self.assertTrue(await limiter.allow(f"user-{index}"))

        self.assertLessEqual(len(limiter._events), 3)
        self.assertIn("user-19", limiter._events)


if __name__ == "__main__":
    unittest.main()
