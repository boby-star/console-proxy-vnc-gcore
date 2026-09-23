import time
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, DummyCookieJar, web

from ..logging_utils import safe_url
from .html_filter import filter_html, rewrite_provider_identity, rewrite_root_relative_urls


HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}
REQUEST_SKIP = HOP_HEADERS | {"host", "cookie", "origin", "referer"}
RESPONSE_SKIP = HOP_HEADERS | {
    "set-cookie", "content-security-policy", "content-security-policy-report-only",
    "x-frame-options",
}


def classify_rewritable_response(content_type, target_url):
    """Identify provider text assets even when the BMC sends a generic MIME type."""
    media_type = content_type.split(";", 1)[0].strip().lower()
    path = urlsplit(target_url).path.lower()
    is_html = media_type in ("text/html", "application/xhtml+xml") or path.endswith(
        (".html", ".htm")
    )
    is_asset = (
        media_type == "text/css"
        or "javascript" in media_type
        or media_type.startswith("text/")
        or media_type in ("application/json", "application/xml", "application/xhtml+xml")
        or media_type.endswith("+json")
        or media_type.endswith("+xml")
        or path.endswith((".js", ".css"))
    ) and not is_html
    return is_html, is_asset


class OvhIpmiHTTPProxy:
    def __init__(self, config, url_builder, cookies, lease):
        self.config = config
        self.url_builder = url_builder
        self.cookies = cookies
        self.lease = lease

    async def proxy(self, request, token, session, set_claim_cookie, claim_id, log):
        await self.lease.refresh(token)
        target = self.url_builder.make_upstream_url(session.upstream_url, request, token)
        public_host = urlsplit(self.config.public_base_url).netloc
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
                        upstream.headers, token, session.upstream_url, public_host
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

                    is_html, is_rewritable_asset = classify_rewritable_response(
                        upstream.headers.get("Content-Type", ""), target
                    )
                    rewrite_body = (
                        (is_html or is_rewritable_asset)
                        and not upstream.headers.get("Content-Encoding")
                    )
                    if rewrite_body:
                        upstream_netloc = urlsplit(session.upstream_url).netloc
                        body = await upstream.read()
                        if is_html:
                            body = filter_html(
                                body, token, upstream_netloc, public_host
                            )
                        else:
                            body = rewrite_provider_identity(
                                body, token, upstream_netloc, public_host
                            )
                            body = rewrite_root_relative_urls(body, token)
                        response.headers.pop("Content-Length", None)
                        response.headers.pop("ETag", None)
                        response.headers.pop("Content-MD5", None)
                        response.headers.pop("Accept-Ranges", None)
                        response.headers.pop("Last-Modified", None)
                        response.headers["Cache-Control"] = "no-store"
                        response.headers["X-Console-Proxy-Rewritten"] = (
                            "html" if is_html else "asset"
                        )
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

    def _response_headers(self, headers, token, upstream_url, public_host):
        result = {}
        upstream_netloc = urlsplit(upstream_url).netloc
        for key, value in headers.items():
            if key.lower() in RESPONSE_SKIP | {
                "server", "via", "x-powered-by", "alt-svc", "report-to", "nel",
            }:
                continue
            if key.lower() == "location":
                value = self.url_builder.rewrite_location(value, token, upstream_url)
            else:
                value = value.replace(upstream_netloc, public_host)
            result[key] = value
        return result