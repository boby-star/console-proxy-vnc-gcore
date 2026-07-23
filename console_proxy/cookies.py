import json
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urlsplit
from aiohttp import ClientSession, DummyCookieJar

class ProviderCookieService:
    def __init__(self, redis, config): self.redis=redis; self.config=config
    async def save(self, token, set_cookie_headers):
        if not set_cookie_headers: return
        key=f"{self.config.cookie_key_prefix}{token}"; raw=await self.redis.get(key); jar={}
        if raw:
            try: jar=json.loads(raw)
            except Exception: jar={}
        for header in set_cookie_headers:
            c=SimpleCookie()
            try: c.load(header)
            except CookieError: continue
            for name,m in c.items():
                if m["max-age"] == "0": jar.pop(name, None)
                else: jar[name]=m.value
        if jar: await self.redis.set(key, json.dumps(jar), ex=self.config.session_ttl_seconds)
        else: await self.redis.delete(key)
    async def build_header(self, token, client_cookie_header):
        raw=await self.redis.get(f"{self.config.cookie_key_prefix}{token}"); jar={}
        if raw:
            try: jar.update(json.loads(raw))
            except Exception: pass
        if client_cookie_header:
            c=SimpleCookie()
            try:
                c.load(client_cookie_header)
                for n,m in c.items(): jar[n]=m.value
            except CookieError: pass
        return "; ".join(f"{n}={v}" for n,v in jar.items()) if jar else None
    def rewrite_for_client(self, headers, token):
        out=[]
        for header in headers:
            c=SimpleCookie()
            try: c.load(header)
            except CookieError: continue
            for n,m in c.items():
                parts=[f"{n}={m.coded_value}", f"Path=/p/{token}/", "Secure", "SameSite=None"]
                if m["max-age"]: parts.append(f"Max-Age={m['max-age']}")
                if m["expires"]: parts.append(f"Expires={m['expires']}")
                if m["httponly"]: parts.append("HttpOnly")
                out.append("; ".join(parts))
        return out
    async def stored_for_client(self, token):
        raw=await self.redis.get(f"{self.config.cookie_key_prefix}{token}")
        if not raw: return []
        try: jar=json.loads(raw)
        except Exception: return []
        out=[]
        for n,v in jar.items():
            c=SimpleCookie(); c[n]=v; c[n]["path"]=f"/p/{token}/"; c[n]["secure"]=True; c[n]["httponly"]=True; c[n]["samesite"]="None"; out.append(c.output(header="").strip())
        return out
    async def prefetch(self, token, upstream_url, log=None):
        if not self.config.prefetch_provider_cookies or urlsplit(upstream_url).scheme not in ("http","https"): return
        p=urlsplit(upstream_url); headers={"Host":p.netloc,"User-Agent":"Mozilla/5.0 console-proxy","Accept":"text/html,*/*"}
        try:
            async with ClientSession(timeout=self.config.http_timeout, cookie_jar=DummyCookieJar(), auto_decompress=False) as s:
                async with s.get(upstream_url, headers=headers, allow_redirects=False) as resp:
                    sc=resp.headers.getall("Set-Cookie", []); await self.save(token, sc); await resp.read()
                    if log: log.info("provider_cookie_prefetch", status=resp.status, cookies=len(sc))
        except Exception:
            if log: log.exception("provider_cookie_prefetch_failed")