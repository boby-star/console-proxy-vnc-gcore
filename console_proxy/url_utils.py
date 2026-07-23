from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from aiohttp import web
from .logging_utils import safe_url

class ConsoleModeDetector:
    def detect(self, console_type, upstream_url):
        if console_type:
            mode = str(console_type).lower()
            if mode not in ("vnc", "serial"):
                raise web.HTTPBadRequest(text="console_type must be vnc or serial")
            return mode
        scheme = urlsplit(upstream_url).scheme
        if scheme in ("http", "https"): return "vnc"
        if scheme in ("ws", "wss"): return "serial"
        raise web.HTTPBadRequest(text="Unsupported upstream URL scheme")

class URLValidator:
    def __init__(self, config): self.config = config
    def is_allowed_host(self, host):
        if not host: return False
        host = host.lower().rstrip(".")
        for suffix in self.config.allowed_host_suffixes:
            suffix = suffix.lower().lstrip(".").rstrip(".")
            if host == suffix or host.endswith("." + suffix): return True
        return False
    def validate(self, url, mode, log=None):
        p = urlsplit(url)
        if not p.netloc or not p.hostname: raise web.HTTPBadRequest(text="Invalid upstream URL")
        if p.username or p.password: raise web.HTTPBadRequest(text="Credentials in upstream URL are not allowed")
        allowed = ("http", "https") if mode == "vnc" else ("ws", "wss")
        if p.scheme not in allowed:
            if log: log.warning("invalid_scheme", mode=mode, scheme=p.scheme, upstream=safe_url(url))
            raise web.HTTPBadRequest(text=f"Invalid upstream URL scheme for {mode}")
        if mode == "serial" and p.scheme == "ws" and log:
            log.warning("serial_insecure_ws", upstream=safe_url(url))
        if not self.is_allowed_host(p.hostname):
            if log: log.warning("host_not_allowed", host=p.hostname, upstream=safe_url(url))
            raise web.HTTPForbidden(text="Upstream host is not allowed")
        return url

class URLBuilder:
    def __init__(self, config, validator): self.config=config; self.validator=validator
    def build_public_vnc_url(self, token, upstream_url):
        p=urlsplit(upstream_url); public_path=f"/p/{token}{p.path or '/'}"; pairs=[]
        for k,v in parse_qsl(p.query, keep_blank_values=True):
            if k=="path": v = f"p/{token}/" + (v if v.startswith("?") else v.lstrip("/"))
            pairs.append((k,v))
        q=urlencode(pairs); return f"{self.config.public_base_url}{public_path}"+(f"?{q}" if q else "")
    def build_public_serial_url(self, token): return f"{self.config.public_base_url}/p/{token}/serial"
    def strip_prefix(self, raw_path, token):
        prefix=f"/p/{token}"; stripped=raw_path[len(prefix):] if raw_path.startswith(prefix) else None
        if stripped is None: raise web.HTTPBadRequest(text="Invalid proxy path")
        return stripped if stripped.startswith("/") else ("/"+stripped if stripped else "/")
    def make_upstream_url(self, upstream_url, request, token, ws=False):
        p=urlsplit(upstream_url); path=self.strip_prefix(request.rel_url.raw_path, token); q=p.query if (not ws and path==(p.path or "/")) else request.rel_url.raw_query_string
        scheme = ("wss" if p.scheme=="https" else "ws") if ws else p.scheme
        return urlunsplit((scheme,p.netloc,path,q,""))
    def upstream_origin(self, upstream_url):
        p=urlsplit(upstream_url); scheme={"wss":"https","ws":"http"}.get(p.scheme,p.scheme)
        return f"{scheme}://{p.netloc}"
    def rewrite_location(self, location, token, upstream_url):
        absolute=urljoin(upstream_url, location); p=urlsplit(absolute)
        return self.build_public_vnc_url(token, absolute) if p.hostname and self.validator.is_allowed_host(p.hostname) else location