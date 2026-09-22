from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from aiohttp import web


class OvhIpmiURLValidator:
    HOST_SUFFIX = "ipmi.ovh.net"

    def is_allowed_host(self, hostname):
        if not hostname:
            return False
        hostname = hostname.lower().rstrip(".")
        return hostname == self.HOST_SUFFIX or hostname.endswith("." + self.HOST_SUFFIX)

    def validate(self, url):
        parsed = urlsplit(url)
        if not parsed.netloc or not parsed.hostname:
            raise web.HTTPBadRequest(text="Invalid upstream URL")
        if parsed.username or parsed.password:
            raise web.HTTPBadRequest(text="Credentials in upstream URL are not allowed")
        if parsed.scheme not in ("http", "https"):
            raise web.HTTPBadRequest(text="Invalid upstream URL scheme for ipmi")
        if not self.is_allowed_host(parsed.hostname):
            raise web.HTTPForbidden(text="Upstream host is not allowed")
        return url


class OvhIpmiURLBuilder:
    def __init__(self, public_base_url, validator):
        self.public_base_url = public_base_url
        self.validator = validator

    def build_public_url(self, token, upstream_url):
        parsed = urlsplit(upstream_url)
        path = f"/ipmi/{token}{parsed.path or '/'}"
        query = urlencode(parse_qsl(parsed.query, keep_blank_values=True))
        return f"{self.public_base_url}{path}" + (f"?{query}" if query else "")

    def make_upstream_url(self, upstream_url, request, token, websocket=False):
        parsed = urlsplit(upstream_url)
        prefix = f"/ipmi/{token}"
        raw_path = request.rel_url.raw_path
        if not raw_path.startswith(prefix):
            raise web.HTTPBadRequest(text="Invalid IPMI proxy path")
        path = raw_path[len(prefix):] or "/"
        if not path.startswith("/"):
            path = "/" + path
        query = parsed.query if not websocket and path == (parsed.path or "/") else request.rel_url.raw_query_string
        scheme = ("wss" if parsed.scheme == "https" else "ws") if websocket else parsed.scheme
        return urlunsplit((scheme, parsed.netloc, path, query, ""))

    def upstream_origin(self, upstream_url):
        parsed = urlsplit(upstream_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def rewrite_location(self, location, token, upstream_url):
        absolute = urljoin(upstream_url, location)
        parsed = urlsplit(absolute)
        if parsed.scheme in ("http", "https") and self.validator.is_allowed_host(parsed.hostname):
            return self.build_public_url(token, absolute)
        return location