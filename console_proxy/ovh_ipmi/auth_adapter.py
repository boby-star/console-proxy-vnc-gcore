from urllib.parse import parse_qs, urlencode, urlsplit

from aiohttp import web


class AsrockAuthAdapter:
    """Normalize the fragile ASRock viewer login request.

    The public bootstrap URL is the authoritative source for the one-time
    credential. Some ASRock frontends lose that credential while moving from
    the prefixed bootstrap URL to root-relative ``/viewer.html``. Passing the
    browser-produced body then makes every protected API return 401.
    """

    LOGIN_PATH = "/api/viewerlogin"

    def prepare_request(self, request, upstream_url, upstream_path, headers, body):
        if request.method != "POST" or upstream_path != self.LOGIN_PATH:
            return headers, body

        values = parse_qs(urlsplit(upstream_url).query, keep_blank_values=True)
        token = values.get("token", [None])[0]
        if not token:
            raise web.HTTPBadGateway(text="OVH IPMI bootstrap token is missing")

        # viewer.min.js uses jQuery's default `data: {token: ...}` encoding.
        # Rebuild it from server-side session state rather than trusting browser
        # state or exposing the credential to additional JavaScript.
        body = urlencode({"token": token}).encode("ascii")
        headers = self._without_entity_headers(headers)
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        headers["Content-Length"] = str(len(body))
        return headers, body

    @staticmethod
    def _without_entity_headers(headers):
        return {
            name: value
            for name, value in headers.items()
            if name.lower() not in {"content-length", "content-type"}
        }