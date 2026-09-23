import secrets

from aiohttp import web

from ..logging_utils import RequestLogger, mask_token, safe_url
from ..models import ConsoleSession


class OvhIpmiRegisterHandler:
    def __init__(self, config, validator, url_builder, store, cookies):
        self.config = config
        self.validator = validator
        self.url_builder = url_builder
        self.store = store
        self.cookies = cookies

    async def __call__(self, request, payload):
        log = RequestLogger(request)
        log.info("ovh_ipmi_register_start")
        if (
            self.config.register_api_token
            and request.headers.get("X-Proxy-Token") != self.config.register_api_token
        ):
            log.warning("auth_failure")
            raise web.HTTPUnauthorized(text="Invalid proxy token")
        upstream = payload.get("upstream_url")
        if not upstream:
            raise web.HTTPBadRequest(text="upstream_url is required")
        self.validator.validate(upstream)
        token = secrets.token_urlsafe(24)
        session = ConsoleSession.create(
            "ipmi", "ovh", upstream,
            self.config.session_ttl_seconds,
        )
        await self.store.save(token, session, log)
        public_url = self.url_builder.build_public_url(token, upstream)
        log.info(
            "ovh_ipmi_register_success",
            token=mask_token(token),
            upstream=safe_url(upstream),
        )
        return web.json_response({
            "token": token,
            "url": public_url,
            "ttl": self.config.session_ttl_seconds,
            "type": "ovh_ipmi",
        })


class OvhIpmiConsoleHandler:
    def __init__(self, store, validator, claims, http_proxy, websocket_bridge):
        self.store = store
        self.validator = validator
        self.claims = claims
        self.http_proxy = http_proxy
        self.websocket_bridge = websocket_bridge

    async def handle(self, request):
        token = request.match_info["token"]
        log = RequestLogger(request)
        session = await self.store.load(token, log)
        if session.mode != "ipmi":
            raise web.HTTPForbidden(text="Invalid OVH IPMI session")
        self.validator.validate(session.upstream_url)
        if request.headers.get("Upgrade", "").lower() == "websocket":
            await self.claims.require_existing_claim(request, token, log)
            return await self.websocket_bridge.bridge(request, token, session, log)
        should_set_claim, claim_id = await self.claims.ensure_claimed(request, token, log)
        return await self.http_proxy.proxy(
            request, token, session, should_set_claim, claim_id, log
        )