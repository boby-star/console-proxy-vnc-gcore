import hashlib, logging, sys, uuid
from urllib.parse import urlsplit, parse_qsl
from aiohttp import web

SENSITIVE_HEADERS = {"cookie", "x-proxy-token", "authorization"}

class SafeAccessLogger(AbstractAccessLogger):
    """Access logger that never writes query-string credentials."""

    def log(self, request, response, time):
        self.logger.info(
            "http_access remote_ip=%s method=%s path=%s status=%s "
            "duration_ms=%s user_agent=%s",
            request.remote,
            request.method,
            request.path,
            response.status,
            int(time * 1000),
            request.headers.get("User-Agent", ""),
        )

def setup_logging():
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(asctime)s level=%(levelname)s event=%(message)s")

def mask_token(value: str | None) -> str | None:
    if not value: return value
    if len(value) <= 10: return hashlib.sha256(value.encode()).hexdigest()[:12]
    return f"{value[:4]}...{value[-4:]}#{hashlib.sha256(value.encode()).hexdigest()[:8]}"

def safe_url(url: str | None) -> str | None:
    if not url: return url
    try:
        p = urlsplit(url)
        keys = [k for k, _ in parse_qsl(p.query, keep_blank_values=True)]
        q = "&".join(f"{k}=***" for k in keys)
        return f"{p.scheme}://{p.netloc}{p.path}" + (f"?{q}" if q else "")
    except Exception:
        return "<invalid-url>"

class RequestLogger:
    def __init__(self, request: web.Request | None = None, **ctx):
        self.logger = logging.getLogger("console_proxy")
        self.ctx = ctx
        if request is not None:
            self.ctx.update({
                "request_id": request.get("request_id"), "remote_ip": request.remote,
                "method": request.method, "path": request.path,
                "user_agent": request.headers.get("User-Agent", ""),
            })
    def _fmt(self, event, **kw):
        data = {**self.ctx, **kw}
        parts = [f"event={event}"] + [f"{k}={v}" for k,v in data.items() if v is not None]
        return " ".join(parts)
    def info(self, event, **kw): self.logger.info(self._fmt(event, **kw))
    def warning(self, event, **kw): self.logger.warning(self._fmt(event, **kw))
    def error(self, event, **kw): self.logger.error(self._fmt(event, **kw))
    def exception(self, event, **kw): self.logger.exception(self._fmt(event, **kw))

@web.middleware
async def request_id_middleware(request, handler):
    request["request_id"] = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    log = RequestLogger(request)
    try:
        resp = await handler(request)
        resp.headers["X-Request-ID"] = request["request_id"]
        return resp
    except web.HTTPException as exc:
        log.warning("request_failed", status=exc.status, reason=exc.reason)
        raise
    except Exception:
        log.exception("request_unhandled_error")
        raise