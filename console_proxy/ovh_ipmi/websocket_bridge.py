import asyncio
import time
from urllib.parse import urlsplit

from aiohttp import ClientError, WSMsgType, WSServerHandshakeError, web

from ..logging_utils import safe_url
from .browser_session import IPMI_BROWSER_COOKIE


class OvhIpmiWebSocketBridge:
    def __init__(self, config, url_builder, cookies, lease, client):
        self.config = config
        self.url_builder = url_builder
        self.cookies = cookies
        self.lease = lease
        self.client = client

    async def bridge(self, request, token, session, log, prefixed=True):
        browser_id = request.cookies.get(IPMI_BROWSER_COOKIE)
        await self.lease.refresh(token, browser_id)
        target = self.url_builder.make_upstream_url(
            session.upstream_url, request, token, websocket=True, prefixed=prefixed
        )
        cookie = await self.cookies.build_header(token, request.headers.get("Cookie"))
        headers = self._headers(request, session.upstream_url, cookie)
        protocols = self._protocols(request)
        log.info(
            "ovh_ipmi_ws_connect_start",
            upstream=safe_url(target),
            attempt_protocols=protocols or "none",
        )
        try:
            kwargs = {
                "headers": headers,
                "heartbeat": 30,
                "max_msg_size": 0,
                "compress": 0,
            }
            if protocols:
                kwargs["protocols"] = protocols
            async with self.client.ws_connect(target, **kwargs) as upstream:
                await self.cookies.save(
                    token, upstream._response.headers.getall("Set-Cookie", [])
                )
                downstream_protocols = (
                    (upstream.protocol,) if upstream.protocol else ()
                )
                downstream = web.WebSocketResponse(
                    heartbeat=30,
                    max_msg_size=0,
                    protocols=downstream_protocols,
                )
                await downstream.prepare(request)
                log.info(
                    "ovh_ipmi_ws_connect_success",
                    selected_subprotocol=upstream.protocol,
                )
                return await self._pump(
                    downstream, upstream, token, browser_id, log
                )
        except WSServerHandshakeError as error:
            log.warning(
                "ovh_ipmi_ws_connect_failure",
                status=error.status,
                message=error.message,
                upstream=safe_url(target),
            )
            raise web.HTTPBadGateway(
                text=f"Upstream IPMI WebSocket handshake failed: {error.status}"
            )
        except ClientError:
            log.exception("ovh_ipmi_ws_connect_failure", upstream=safe_url(target))
            raise web.HTTPBadGateway(text="Upstream IPMI WebSocket connection failed")

    async def _pump(self, downstream, upstream, token, browser_id, log):
        started = time.monotonic()
        lease_task = asyncio.create_task(self.lease.maintain(token, browser_id))

        async def client_to_upstream():
            async for message in downstream:
                if message.type == WSMsgType.TEXT:
                    await upstream.send_str(message.data)
                elif message.type == WSMsgType.BINARY:
                    await upstream.send_bytes(message.data)
                elif message.type == WSMsgType.PING:
                    await upstream.ping()
                elif message.type == WSMsgType.PONG:
                    await upstream.pong()
                elif message.type == WSMsgType.CLOSE:
                    await upstream.close()
                    break

        async def upstream_to_client():
            async for message in upstream:
                if message.type == WSMsgType.TEXT:
                    await downstream.send_str(message.data)
                elif message.type == WSMsgType.BINARY:
                    await downstream.send_bytes(message.data)
                elif message.type == WSMsgType.PING:
                    await downstream.ping()
                elif message.type == WSMsgType.PONG:
                    await downstream.pong()
                elif message.type == WSMsgType.CLOSE:
                    await downstream.close()
                    break

        tasks = [
            asyncio.create_task(client_to_upstream()),
            asyncio.create_task(upstream_to_client()),
        ]
        try:
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        finally:
            await self.lease.stop(lease_task)
        log.info(
            "ovh_ipmi_ws_closed",
            client_close_code=downstream.close_code,
            upstream_close_code=upstream.close_code,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        if not downstream.closed:
            await downstream.close()
        return downstream

    def _headers(self, request, upstream_url, cookie):
        parsed = urlsplit(upstream_url)
        skip = {
            "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
            "te", "trailers", "transfer-encoding", "upgrade", "host", "cookie",
            "origin", "referer", "sec-websocket-key", "sec-websocket-version",
            "sec-websocket-extensions", "sec-websocket-accept", "sec-websocket-protocol",
        }
        headers = {
            key: value for key, value in request.headers.items()
            if key.lower() not in skip
        }
        headers["Host"] = parsed.netloc
        headers["Origin"] = self.url_builder.upstream_origin(upstream_url)
        if cookie:
            headers["Cookie"] = cookie
        return headers

    @staticmethod
    def _protocols(request):
        offered = request.headers.get("Sec-WebSocket-Protocol", "")
        return tuple(
            protocol.strip() for protocol in offered.split(",")
            if protocol.strip() in ("binary", "base64")
        )