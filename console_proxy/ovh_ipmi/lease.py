import asyncio
from contextlib import suppress

from .browser_session import OvhIpmiBrowserSession


class OvhIpmiLease:
    """Keep an IPMI session alive while its HTTP or WebSocket traffic is active."""

    def __init__(self, redis, config):
        self.redis = redis
        self.config = config

    async def refresh(self, token, browser_id=None):
        keys = (
            f"{self.config.session_key_prefix}{token}",
            f"{self.config.cookie_key_prefix}{token}",
            f"{self.config.claim_key_prefix}{token}",
        )
        async with self.redis.pipeline(transaction=False) as pipeline:
            for key in keys:
                pipeline.expire(key, self.config.session_ttl_seconds)
            if browser_id:
                pipeline.expire(
                    OvhIpmiBrowserSession.KEY_PREFIX + browser_id,
                    self.config.session_ttl_seconds,
                )
            await pipeline.execute()

    async def maintain(self, token, browser_id=None):
        interval = max(30, self.config.session_ttl_seconds // 3)
        try:
            while True:
                await asyncio.sleep(interval)
                await self.refresh(token, browser_id)
        except asyncio.CancelledError:
            raise

    async def stop(self, task):
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task