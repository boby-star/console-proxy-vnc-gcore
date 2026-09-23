import secrets
from aiohttp import web
from ..logging_utils import RequestLogger, mask_token, safe_url
from ..models import ConsoleSession

class RegisterHandler:
    def __init__(self, config, detector, validator, url_builder, store, cookies): self.config=config; self.detector=detector; self.validator=validator; self.url_builder=url_builder; self.store=store; self.cookies=cookies
    async def __call__(self, request, payload=None, response_type=None):
        log=RequestLogger(request); log.info("register_start")
        if self.config.register_api_token and request.headers.get("X-Proxy-Token") != self.config.register_api_token:
            log.warning("auth_failure"); raise web.HTTPUnauthorized(text="Invalid proxy token")
        if payload is None:
            try: payload=await request.json()
            except Exception:
                log.warning("invalid_json"); raise web.HTTPBadRequest(text="Invalid JSON body")
        upstream=payload.get("upstream_url")
        if not upstream:
            log.warning("invalid_missing_upstream_url"); raise web.HTTPBadRequest(text="upstream_url is required")
        mode=self.detector.detect(payload.get("console_type"), upstream); provider="gcore"
        self.validator.validate(upstream, mode, log); log.info("console_mode_detected", mode=mode, provider=provider, upstream=safe_url(upstream))
        token=secrets.token_urlsafe(24); session=ConsoleSession.create(mode, provider, upstream, self.config.session_ttl_seconds); await self.store.save(token, session, log)
        if mode == "vnc":
            await self.cookies.prefetch(token, upstream, log); public=self.url_builder.build_public_vnc_url(token, upstream)
        else: public=self.url_builder.build_public_serial_url(token)
        log.info("register_success", token=mask_token(token), mode=mode)
        return web.json_response({"token":token,"url":public,"ttl":self.config.session_ttl_seconds,"type":response_type or mode})