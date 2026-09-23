from urllib.parse import urlsplit

from aiohttp import web


class ConsoleRegisterDispatcher:
    """Dispatch the public console types to their isolated implementations."""

    def __init__(self, default_handler, ovh_ipmi_handler):
        self.default_handler = default_handler
        self.ovh_ipmi_handler = ovh_ipmi_handler

    async def __call__(self, request):
        try:
            payload = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text="Invalid JSON body")

        console_type = str(payload.get("console_type", "")).lower()
        if console_type == "gcore_vnc":
            legacy_payload = dict(payload)
            legacy_payload["console_type"] = "vnc"
            legacy_payload.pop("provider", None)
            return await self.default_handler(
                request, legacy_payload, "gcore_vnc"
            )
        if console_type != "ovh_ipmi":
            raise web.HTTPBadRequest(
                text="console_type must be gcore_vnc or ovh_ipmi"
            )

        ipmi_payload = dict(payload)
        ipmi_payload.pop("provider", None)
        upstream_url = ipmi_payload.get("upstream_url")
        if not upstream_url:
            return await self.ovh_ipmi_handler(request, ipmi_payload)
        try:
            hostname = urlsplit(upstream_url).hostname
        except ValueError:
            hostname = None
        if not self._is_ovh_ipmi_host(hostname):
            raise web.HTTPForbidden(text="Upstream host is not allowed")

        return await self.ovh_ipmi_handler(request, ipmi_payload)

    @staticmethod
    def _is_ovh_ipmi_host(hostname):
        if not hostname:
            return False
        hostname = hostname.lower().rstrip(".")
        return hostname == "ipmi.ovh.net" or hostname.endswith(".ipmi.ovh.net")