from aiohttp import web
from .models import ConsoleSession
from .logging_utils import mask_token

class SessionStore:
    def __init__(self, redis, config): self.redis=redis; self.config=config
    async def save(self, token, session, log=None):
        await self.redis.set(f"{self.config.session_key_prefix}{token}", session.to_json(), ex=session.ttl)
        if log: log.info("redis_session_save", token=mask_token(token), mode=session.mode)
    async def load(self, token, log=None):
        raw=await self.redis.get(f"{self.config.session_key_prefix}{token}")
        if not raw:
            if log: log.warning("session_not_found", token=mask_token(token))
            raise web.HTTPGone(text="Console session expired or not found")
        session=ConsoleSession.from_redis(raw, self.config.session_ttl_seconds)
        if log: log.info("redis_session_load", token=mask_token(token), mode=session.mode, legacy=session.version != 2)
        return session