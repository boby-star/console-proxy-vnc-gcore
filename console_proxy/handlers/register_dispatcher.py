from urllib.parse import urlsplit

from aiohttp import web


class ConsoleRegisterDispatcher:
    """Route OVH IPMI registrations without changing the legacy handler."""

    def __init__(self, default_handler, ovh_ipmi_handler):
        self.default_handler = default_handler
        self.ovh_ipmi_handler = ovh_ipmi_handler

    async def __call__(self, request):
        try:
            payload = await request.json()
        except Exception:
            return await self.default_handler(request)

        if str(payload.get("console_type", "")).lower() != "ipmi":
            return await self.default_handler(request)

        upstream_url = payload.get("upstream_url") or payload.get("url")
        if not upstream_url:
            return await self.ovh_ipmi_handler(request, payload)
        try:
            hostname = urlsplit(upstream_url).hostname
        except ValueError:
            hostname = None
        if not self._is_ovh_ipmi_host(hostname):
            raise web.HTTPForbidden(text="Upstream host is not allowed")

        return await self.ovh_ipmi_handler(request, payload)

    @staticmethod
    def _is_ovh_ipmi_host(hostname):
        if not hostname:
            return False
        hostname = hostname.lower().rstrip(".")
        return hostname == "ipmi.ovh.net" or hostname.endswith(".ipmi.ovh.net")