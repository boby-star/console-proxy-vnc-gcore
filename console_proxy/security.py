import secrets
from aiohttp import web
from .logging_utils import mask_token

class ClaimService:
    def __init__(self, redis, config): self.redis=redis; self.config=config
    async def ensure_claimed(self, request, token, log=None):
        claim_key=f"{self.config.claim_key_prefix}{token}"; client=request.cookies.get(self.config.claim_cookie_name); stored=await self.redis.get(claim_key)
        if stored:
            if client and client == stored:
                if log: log.info("claim_matched", token=mask_token(token))
                return False, stored
            if log: log.warning("claim_denied", token=mask_token(token), has_cookie=bool(client))
            raise web.HTTPForbidden(text="Console link has already been used")
        new=secrets.token_urlsafe(24); ok=await self.redis.set(claim_key, new, ex=self.config.session_ttl_seconds, nx=True)
        if ok:
            if log: log.info("claim_created", token=mask_token(token))
            return True, new
        stored=await self.redis.get(claim_key)
        if client and stored and client == stored: return False, stored
        if log: log.warning("claim_denied", token=mask_token(token), race=True)
        raise web.HTTPForbidden(text="Console link has already been used")
    async def require_existing_claim(self, request, token, log=None):
        stored=await self.redis.get(f"{self.config.claim_key_prefix}{token}"); client=request.cookies.get(self.config.claim_cookie_name)
        if stored and client and stored == client:
            if log: log.info("claim_matched", token=mask_token(token)); return
        if log: log.warning("claim_denied", token=mask_token(token), ws_first=not bool(stored), has_cookie=bool(client))
        raise web.HTTPForbidden(text="Console link has not been claimed by this browser")