import time
from urllib.parse import urlsplit

from aiohttp import ClientError, web

from ..logging_utils import safe_url
from .browser_session import IPMI_BROWSER_COOKIE
from .html_filter import filter_html, rewrite_provider_identity


HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}
REQUEST_SKIP = HOP_HEADERS | {"host", "cookie", "origin", "referer"}
REQUEST_SKIP |= {
    "cf-connecting-ip",
    "cf-ipcountry",
    "cf-ray",
    "forwarded",
    "true-client-ip",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-port",
    "x-forwarded-proto",
    "x-real-ip",
}
RESPONSE_SKIP = HOP_HEADERS | {
    "set-cookie", "content-security-policy", "content-security-policy-report-only",
    "x-frame-options",
}


def is_forwardable_request_header(name):
    lowered = name.lower()
    return lowered not in REQUEST_SKIP and not lowered.startswith("x-forwarded-")


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
    def __init__(self, config, url_builder, cookies, lease, client, auth_adapter=None):
        self.config = config
        self.url_builder = url_builder
        self.cookies = cookies
        self.lease = lease
        self.client = client
        self.auth_adapter = auth_adapter

    async def proxy(self, request, token, session, browser_id, log, prefixed=True):
        await self.lease.refresh(token, browser_id)
        target = self.url_builder.make_upstream_url(
            session.upstream_url, request, token, prefixed=prefixed
        )
        upstream_path = urlsplit(target).path
        session_path = urlsplit(session.upstream_url).path or "/"
        is_bootstrap = prefixed and upstream_path == session_path
        bootstrap_cookies = []
        if is_bootstrap:
            bootstrap_cookies = await self.cookies.initialize_bootstrap(
                token, session.upstream_url
            )
        public_host = urlsplit(self.config.public_base_url).netloc
        started = time.monotonic()
        cookie = await self.cookies.build_header(token, request.headers.get("Cookie"))
        headers = self._request_headers(request, session.upstream_url, cookie, token)
        # ASRock's embedded HTTP server does not reliably parse chunked request
        # bodies. aiohttp uses chunked transfer encoding for an async iterator,
        # which made /api/viewerlogin see an empty token and return 401. HTTP
        # control requests are bounded by aiohttp's client_max_size; Virtual
        # Media itself is transferred by WebSocket and remains streaming.
        data = await request.read() if request.can_read_body else None
        auth_body_normalized = bool(
            self.auth_adapter
            and request.method == "POST"
            and upstream_path == self.auth_adapter.LOGIN_PATH
        )
        if self.auth_adapter:
            headers, data = self.auth_adapter.prepare_request(
                request,
                session.upstream_url,
                upstream_path,
                headers,
                data,
            )
        log.info(
            "ovh_ipmi_http_start",
            upstream=safe_url(target),
            has_provider_cookie=bool(cookie),
            has_csrf_header=any(key.lower() == "x-csrftoken" for key in headers),
            request_body_bytes=len(data) if data is not None else 0,
            auth_body_normalized=auth_body_normalized,
        )
        try:
            async with self.client.request(
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
                for header in bootstrap_cookies:
                    response.headers.add("Set-Cookie", header)
                response.set_cookie(
                    IPMI_BROWSER_COOKIE,
                    browser_id,
                    path="/",
                    max_age=self.config.session_ttl_seconds,
                    secure=True,
                    httponly=True,
                    samesite="Lax",
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
                        body = filter_html(body, upstream_netloc, public_host)
                    else:
                        body = rewrite_provider_identity(
                            body, upstream_netloc, public_host
                        )
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

    def _request_headers(self, request, upstream_url, cookie, token=None):
        parsed = urlsplit(upstream_url)
        headers = {
            key: value for key, value in request.headers.items()
            if is_forwardable_request_header(key)
        }
        headers["Host"] = parsed.netloc
        headers["Accept-Encoding"] = "identity"
        if request.headers.get("Origin"):
            headers["Origin"] = self.url_builder.upstream_origin(upstream_url)
        if request.headers.get("Referer"):
            referer = urlsplit(request.headers["Referer"])
            referer_path = referer.path or "/"
            prefix = f"/ipmi/{token}" if token else None
            if prefix and referer_path.startswith(prefix):
                referer_path = referer_path[len(prefix):] or "/"
            headers["Referer"] = self.url_builder.upstream_origin(upstream_url) + (
                referer_path
            ) + (("?" + referer.query) if referer.query else "")
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