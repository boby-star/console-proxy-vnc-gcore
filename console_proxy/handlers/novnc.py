from aiohttp import web
from ..logging_utils import RequestLogger

class NoVncConsoleHandler:
    def __init__(self, store, validator, claims, http_proxy, ws_bridge): self.store=store; self.validator=validator; self.claims=claims; self.http_proxy=http_proxy; self.ws_bridge=ws_bridge
    async def handle(self, request):
        token=request.match_info["token"]; log=RequestLogger(request)
        if request.headers.get("Upgrade", "").lower() == "websocket":
            session=await self.store.load(token, log); self.validator.validate(session.upstream_url, "vnc", log); await self.claims.require_existing_claim(request, token, log); return await self.ws_bridge.bridge_novnc(request, token, session, log)
        should, claim=await self.claims.ensure_claimed(request, token, log); session=await self.store.load(token, log); self.validator.validate(session.upstream_url, "vnc", log); return await self.http_proxy.proxy(request, token, session, should, claim, log)