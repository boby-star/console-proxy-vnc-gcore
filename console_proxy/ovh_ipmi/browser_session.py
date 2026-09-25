import secrets

from aiohttp import web


IPMI_BROWSER_COOKIE = "console_proxy_ipmi"


class OvhIpmiBrowserSession:
    """Bind one browser to one active IPMI session on the shared origin."""

    COOKIE_NAME = IPMI_BROWSER_COOKIE
    KEY_PREFIX = "console:ipmi:browser:"

    def __init__(self, redis, config):
        self.redis = redis
        self.config = config

    async def bind(self, request, token):
        browser_id = request.cookies.get(self.COOKIE_NAME)
        if not browser_id:
            browser_id = secrets.token_urlsafe(24)
        await self.redis.set(
            self.KEY_PREFIX + browser_id,
            token,
            ex=self.config.session_ttl_seconds,
        )
        return browser_id

    async def resolve(self, request):
        browser_id = request.cookies.get(self.COOKIE_NAME)
        if not browser_id:
            raise web.HTTPNotFound(text="IPMI session not found")
        token = await self.redis.get(self.KEY_PREFIX + browser_id)
        if not token:
            raise web.HTTPGone(text="Console session expired or not found")
        return token

    async def refresh(self, browser_id):
        if browser_id:
            await self.redis.expire(
                self.KEY_PREFIX + browser_id,
                self.config.session_ttl_seconds,
            )