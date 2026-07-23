from aiohttp import web
from pathlib import Path
from ..logging_utils import RequestLogger

class SerialConsoleHandler:
    def __init__(self, config, store, validator, claims, ws_bridge): self.config=config; self.store=store; self.validator=validator; self.claims=claims; self.ws_bridge=ws_bridge; self.template=Path(__file__).resolve().parents[1]/"templates"/"serial.html"
    async def page(self, request):
        token=request.match_info["token"]; log=RequestLogger(request); should, claim=await self.claims.ensure_claimed(request, token, log); session=await self.store.load(token, log); self.validator.validate(session.upstream_url, "serial", log)
        html=self.template.read_text().replace("{{ token }}", token)
        resp=web.Response(text=html, content_type="text/html")
        if should and claim: resp.set_cookie(self.config.claim_cookie_name, claim, path=f"/p/{token}/", max_age=self.config.session_ttl_seconds, secure=True, httponly=True, samesite="None")
        return resp
    async def ws(self, request):
        token=request.match_info["token"]; log=RequestLogger(request); session=await self.store.load(token, log); self.validator.validate(session.upstream_url, "serial", log); await self.claims.require_existing_claim(request, token, log); return await self.ws_bridge.bridge_serial(request, token, session, log)