import json
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit

from ..cookies import ProviderCookieService
from .browser_session import IPMI_BROWSER_COOKIE


class OvhIpmiCookieService(ProviderCookieService):
    async def initialize_bootstrap(self, token, upstream_url):
        """Seed the ASRock cookies before the one-time bootstrap request."""
        query = parse_qs(urlsplit(upstream_url).query, keep_blank_values=True)
        provider_token = query.get("token", [None])[0]
        if not provider_token:
            return []
        jar = {
            "QSESSIONID": provider_token,
            "refresh_disable": "1",
        }
        await self.redis.set(
            f"{self.config.cookie_key_prefix}{token}",
            json.dumps(jar),
            ex=self.config.session_ttl_seconds,
            nx=True,
        )
        return self._jar_for_client(jar)

    async def build_header(self, token, client_cookie_header):
        internal_names = {
            self.config.claim_cookie_name,
            IPMI_BROWSER_COOKIE,
        }
        jar = {}
        if client_cookie_header:
            client = SimpleCookie()
            try:
                client.load(client_cookie_header)
            except Exception:
                pass
            else:
                jar.update({
                    name: morsel.value
                    for name, morsel in client.items()
                    if name not in internal_names
                })

        # Redis is scoped by proxy session and is authoritative. In particular,
        # it must override a stale root-scoped QSESSIONID left by another IPMI
        # console on the same public domain.
        raw = await self.redis.get(f"{self.config.cookie_key_prefix}{token}")
        if raw:
            try:
                jar.update(json.loads(raw))
            except Exception:
                pass
        return "; ".join(f"{name}={value}" for name, value in jar.items()) or None

    def rewrite_for_client(self, headers, token):
        rewritten = []
        for header in headers:
            cookie = SimpleCookie()
            try:
                cookie.load(header)
            except Exception:
                continue
            for name, morsel in cookie.items():
                parts = [
                    f"{name}={morsel.coded_value}",
                    "Path=/",
                    "Secure",
                    "SameSite=None",
                ]
                if morsel["max-age"]:
                    parts.append(f"Max-Age={morsel['max-age']}")
                if morsel["expires"]:
                    parts.append(f"Expires={morsel['expires']}")
                if morsel["httponly"]:
                    parts.append("HttpOnly")
                rewritten.append("; ".join(parts))
        return rewritten

    async def stored_for_client(self, token):
        raw = await self.redis.get(f"{self.config.cookie_key_prefix}{token}")
        if not raw:
            return []
        try:
            jar = json.loads(raw)
        except Exception:
            return []
        return self._jar_for_client(jar)

    @staticmethod
    def _jar_for_client(jar):
        result = []
        for name, value in jar.items():
            cookie = SimpleCookie()
            cookie[name] = value
            cookie[name]["path"] = "/"
            cookie[name]["secure"] = True
            cookie[name]["samesite"] = "None"
            result.append(cookie.output(header="").strip())
        return result