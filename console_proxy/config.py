import os
from dataclasses import dataclass, field
from aiohttp import ClientTimeout

@dataclass(frozen=True)
class Config:
    redis_url: str = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "https://vnc-zomro.com").rstrip("/")
    register_api_token: str = os.getenv("REGISTER_API_TOKEN", "")
    session_ttl_seconds: int = int(os.getenv("SESSION_TTL_SECONDS", "3000"))
    prefetch_provider_cookies: bool = os.getenv("PREFETCH_PROVIDER_COOKIES", "true").lower() in ("1", "true", "yes")
    ovh_upstream_connection_limit: int = int(os.getenv("OVH_UPSTREAM_CONNECTION_LIMIT", "2000"))
    allowed_host_suffixes: list[str] = field(default_factory=lambda: [
        s.strip().lower() for s in os.getenv("ALLOWED_HOST_SUFFIXES", "cloud.gcore.com,ipmi.ovh.net").split(",") if s.strip()
    ])
    session_key_prefix: str = "console:session:"
    cookie_key_prefix: str = "console:cookies:"
    claim_key_prefix: str = "console:claim:"
    claim_cookie_name: str = "console_proxy_claim"
    http_timeout: ClientTimeout = field(default_factory=lambda: ClientTimeout(total=None, sock_connect=30, sock_read=None))