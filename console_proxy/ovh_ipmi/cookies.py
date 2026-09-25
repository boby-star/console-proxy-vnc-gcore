from http.cookies import SimpleCookie

from ..cookies import ProviderCookieService
from .browser_session import IPMI_BROWSER_COOKIE


class OvhIpmiCookieService(ProviderCookieService):
    async def build_header(self, token, client_cookie_header):
        header = await super().build_header(token, client_cookie_header)
        if not header:
            return None
        internal_names = {
            self.config.claim_cookie_name,
            IPMI_BROWSER_COOKIE,
        }
        cookies = [part.strip() for part in header.split(";")]
        cookies = [
            part for part in cookies
            if part.partition("=")[0] not in internal_names
        ]
        return "; ".join(cookies) or None

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
        import json
        try:
            jar = json.loads(raw)
        except Exception:
            return []
        result = []
        for name, value in jar.items():
            cookie = SimpleCookie()
            cookie[name] = value
            cookie[name]["path"] = "/"
            cookie[name]["secure"] = True
            cookie[name]["samesite"] = "None"
            result.append(cookie.output(header="").strip())
        return result