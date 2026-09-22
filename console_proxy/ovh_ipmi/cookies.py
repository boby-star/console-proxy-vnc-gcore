from http.cookies import SimpleCookie

from ..cookies import ProviderCookieService


class OvhIpmiCookieService(ProviderCookieService):
    async def build_header(self, token, client_cookie_header):
        header = await super().build_header(token, client_cookie_header)
        if not header:
            return None
        claim_prefix = self.config.claim_cookie_name + "="
        cookies = [part.strip() for part in header.split(";")]
        cookies = [part for part in cookies if not part.startswith(claim_prefix)]
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
                    f"Path=/ipmi/{token}/",
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
            cookie[name]["path"] = f"/ipmi/{token}/"
            cookie[name]["secure"] = True
            cookie[name]["httponly"] = True
            cookie[name]["samesite"] = "None"
            result.append(cookie.output(header="").strip())
        return result