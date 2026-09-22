import time
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, DummyCookieJar, web

from ..logging_utils import safe_url
from .html_filter import filter_html, rewrite_root_relative_urls


HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}
REQUEST_SKIP = HOP_HEADERS | {"host", "cookie", "origin", "referer"}
RESPONSE_SKIP = HOP_HEADERS | {
    "set-cookie", "content-security-policy", "content-security-policy-report-only",
    "x-frame-options",
}


class OvhIpmiHTTPProxy:
    def __init__(self, config, url_builder, cookies, lease):
        self.config = config
        self.url_builder = url_builder
        self.cookies = cookies
        self.lease = lease

    async def proxy(self, request, token, session, set_claim_cookie, claim_id, log):
        await self.lease.refresh(token)
        target = self.url_builder.make_upstream_url(session.upstream_url, request, token)
        started = time.monotonic()
        log.info("ovh_ipmi_http_start", upstream=safe_url(target))
        cookie = await self.cookies.build_header(token, request.headers.get("Cookie"))
        headers = self._request_headers(request, session.upstream_url, cookie)
        data = request.content.iter_chunked(65536) if request.can_read_body else None
        try:
            async with ClientSession(
                timeout=self.config.http_timeout,
                cookie_jar=DummyCookieJar(),
                auto_decompress=False,
            ) as client:
                async with client.request(
                    request.method,
                    target,
                    headers=headers,
                    data=data,
                    allow_redirects=False,
                ) as upstream:
                    set_cookies = upstream.headers.getall("Set-Cookie", [])
                    await self.cookies.save(token, set_cookies)
                    response_headers = self._response_headers(
                        upstream.headers, token, session.upstream_url
                    )
                    response = web.StreamResponse(
                        status=upstream.status,
                        reason=upstream.reason,
                        headers=response_headers,
                    )
                    for header in self.cookies.rewrite_for_client(set_cookies, token):
                        response.headers.add("Set-Cookie", header)
                    for header in await self.cookies.stored_for_client(token):
                        response.headers.add("Set-Cookie", header)
                    if set_claim_cookie and claim_id:
                        response.set_cookie(
                            self.config.claim_cookie_name,
                            claim_id,
                            path=f"/ipmi/{token}/",
                            max_age=self.config.session_ttl_seconds,
                            secure=True,
                            httponly=True,
                            samesite="None",
                        )

                    content_type = upstream.headers.get("Content-Type", "").lower()
                    is_html = content_type.startswith("text/html")
                    is_rewritable_asset = (
                        content_type.startswith("text/css")
                        or "javascript" in content_type
                        or target.split("?", 1)[0].endswith((".js", ".css"))
                    )
                    rewrite_body = (
                        (is_html or is_rewritable_asset)
                        and not upstream.headers.get("Content-Encoding")
                    )
                    if rewrite_body:
                        hostname = urlsplit(session.upstream_url).hostname
                        body = await upstream.read()
                        if is_html:
                            body = filter_html(body, token, hostname)
                        else:
                            body = rewrite_root_relative_urls(body, token)
                        response.headers.pop("Content-Length", None)
                        response.headers.pop("ETag", None)
                        response.headers.pop("Content-MD5", None)
                        response.headers.pop("Accept-Ranges", None)
                        response.content_length = len(body)

                    await response.prepare(request)
                    if rewrite_body:
                        await response.write(body)
                    else:
                        async for chunk in upstream.content.iter_chunked(65536):
                            await response.write(chunk)
                    await response.write_eof()
                    log.info(
                        "ovh_ipmi_http_done",
                        upstream_status=upstream.status,
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
                    return response
        except ClientError:
            log.exception("ovh_ipmi_http_failure", upstream=safe_url(target))
            raise web.HTTPBadGateway(text="Upstream IPMI HTTP request failed")

    def _request_headers(self, request, upstream_url, cookie):
        parsed = urlsplit(upstream_url)
        headers = {
            key: value for key, value in request.headers.items()
            if key.lower() not in REQUEST_SKIP
        }
        headers["Host"] = parsed.netloc
        headers["Accept-Encoding"] = "identity"
        if request.headers.get("Origin"):
            headers["Origin"] = self.url_builder.upstream_origin(upstream_url)
        if request.headers.get("Referer"):
            headers["Referer"] = upstream_url
        if cookie:
            headers["Cookie"] = cookie
        return headers

    def _response_headers(self, headers, token, upstream_url):
        result = {}
        for key, value in headers.items():
            if key.lower() in RESPONSE_SKIP:
                continue
            if key.lower() == "location":
                value = self.url_builder.rewrite_location(value, token, upstream_url)
            result[key] = value
        return result