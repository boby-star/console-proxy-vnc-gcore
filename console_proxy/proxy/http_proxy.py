import time
from aiohttp import ClientError, ClientSession, DummyCookieJar, web
from ..logging_utils import safe_url

HOP={"connection","keep-alive","proxy-authenticate","proxy-authorization","te","trailers","transfer-encoding","upgrade"}
RESP_SKIP=HOP|{"set-cookie","content-security-policy","content-security-policy-report-only","x-frame-options"}
REQ_SKIP=HOP|{"host","cookie","origin","referer"}
class HTTPProxyService:
    def __init__(self, config, url_builder, cookies): self.config=config; self.url_builder=url_builder; self.cookies=cookies
    async def proxy(self, request, token, session, should_set_claim_cookie, claim_id, log):
        target=self.url_builder.make_upstream_url(session.upstream_url, request, token, ws=False); started=time.monotonic(); log.info("http_proxy_start", upstream=safe_url(target))
        cookie=await self.cookies.build_header(token, request.headers.get("Cookie")); headers=self._headers(request, session.upstream_url, cookie); data=request.content.iter_chunked(65536) if request.can_read_body else None
        try:
            async with ClientSession(timeout=self.config.http_timeout, cookie_jar=DummyCookieJar(), auto_decompress=False) as s:
                async with s.request(request.method, target, headers=headers, data=data, allow_redirects=False) as up:
                    sc=up.headers.getall("Set-Cookie", []); await self.cookies.save(token, sc)
                    resp=web.StreamResponse(status=up.status, reason=up.reason, headers=self._resp_headers(up.headers, token, session.upstream_url))
                    for h in self.cookies.rewrite_for_client(sc, token): resp.headers.add("Set-Cookie", h)
                    for h in await self.cookies.stored_for_client(token): resp.headers.add("Set-Cookie", h)
                    if should_set_claim_cookie and claim_id: resp.set_cookie(self.config.claim_cookie_name, claim_id, path=f"/p/{token}/", max_age=self.config.session_ttl_seconds, secure=True, httponly=True, samesite="None")
                    await resp.prepare(request)
                    async for chunk in up.content.iter_chunked(65536): await resp.write(chunk)
                    await resp.write_eof(); log.info("http_proxy_done", upstream_status=up.status, duration_ms=int((time.monotonic()-started)*1000)); return resp
        except ClientError:
            log.exception("http_proxy_failure", upstream=safe_url(target)); raise web.HTTPBadGateway(text="Upstream HTTP request failed")
    def _headers(self, request, upstream_url, cookie):
        from urllib.parse import urlsplit
        p=urlsplit(upstream_url); h={k:v for k,v in request.headers.items() if k.lower() not in REQ_SKIP}; h["Host"]=p.netloc
        if request.headers.get("Origin"): h["Origin"]=self.url_builder.upstream_origin(upstream_url)
        if request.headers.get("Referer"): h["Referer"]=upstream_url
        if cookie: h["Cookie"]=cookie
        return h
    def _resp_headers(self, headers, token, upstream_url):
        out={}
        for k,v in headers.items():
            if k.lower() in RESP_SKIP: continue
            out[k]=self.url_builder.rewrite_location(v, token, upstream_url) if k.lower()=="location" else v
        return out