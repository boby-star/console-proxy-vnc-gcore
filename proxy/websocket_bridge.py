import asyncio, time
from aiohttp import ClientError, ClientSession, DummyCookieJar, WSMsgType, WSServerHandshakeError, web
from ..logging_utils import safe_url

class WebSocketBridge:
    def __init__(self, config, url_builder, cookies): self.config=config; self.url_builder=url_builder; self.cookies=cookies
    async def bridge_novnc(self, request, token, session, log):
        cookie=await self.cookies.build_header(token, request.headers.get("Cookie")); target=self.url_builder.make_upstream_url(session.upstream_url, request, token, ws=True)
        headers=self._headers(request, session.upstream_url, True, cookie); log.info("ws_connect_start", mode="vnc", upstream=safe_url(target), attempt_protocols="binary")
        try:
            async with ClientSession(timeout=self.config.http_timeout, cookie_jar=DummyCookieJar()) as s:
                async with s.ws_connect(target, headers=headers, heartbeat=30, max_msg_size=0, protocols=("binary",)) as up:
                    await self.cookies.save(token, up._response.headers.getall("Set-Cookie", [])); log.info("ws_connect_success", selected_subprotocol=up.protocol)
                    client=web.WebSocketResponse(heartbeat=30, max_msg_size=0, protocols=("binary",))
                    for h in await self.cookies.stored_for_client(token): client.headers.add("Set-Cookie", h)
                    await client.prepare(request); return await self._pump(client, up, log, binary_to_text=False)
        except WSServerHandshakeError as e:
            log.warning("ws_connect_failure", status=e.status, message=e.message, upstream=safe_url(target)); raise web.HTTPBadGateway(text=f"Upstream WebSocket handshake failed: {e.status}")
        except ClientError:
            log.exception("ws_connect_failure", upstream=safe_url(target)); raise web.HTTPBadGateway(text="Upstream WebSocket connection failed")
    async def bridge_serial(self, request, token, session, log):
        target=session.upstream_url; headers=self._headers(request, session.upstream_url, True, None); attempts=[("binary","base64"),("binary",),None]
        last=None
        for protocols in attempts:
            try:
                log.info("ws_connect_start", mode="serial", upstream=safe_url(target), attempt_protocols=protocols or "none")
                async with ClientSession(timeout=self.config.http_timeout, cookie_jar=DummyCookieJar()) as s:
                    kwargs={"headers":headers,"heartbeat":30,"max_msg_size":0}
                    if protocols: kwargs["protocols"]=protocols
                    async with s.ws_connect(target, **kwargs) as up:
                        log.info("ws_connect_success", selected_subprotocol=up.protocol)
                        client=web.WebSocketResponse(heartbeat=30, max_msg_size=0); await client.prepare(request)
                        return await self._pump(client, up, log, serial=True)
            except WSServerHandshakeError as e:
                last=e; log.warning("ws_connect_failure", status=e.status, message=e.message, attempt_protocols=protocols or "none", upstream=safe_url(target)); continue
            except ClientError as e:
                last=e; log.warning("ws_connect_failure", error=repr(e), attempt_protocols=protocols or "none", upstream=safe_url(target)); continue
        raise web.HTTPBadGateway(text="Upstream WebSocket connection failed")
    async def _pump(self, client, up, log, serial=False, binary_to_text=False):
        started=time.monotonic()
        async def c2u():
            async for msg in client:
                if msg.type == WSMsgType.TEXT:
                    if serial and up.protocol == "binary": await up.send_bytes(msg.data.encode())
                    else: await up.send_str(msg.data)
                elif msg.type == WSMsgType.BINARY: await up.send_bytes(msg.data)
                elif msg.type == WSMsgType.PING: await up.ping()
                elif msg.type == WSMsgType.PONG: await up.pong()
                elif msg.type == WSMsgType.CLOSE: await up.close(); break
        async def u2c():
            async for msg in up:
                if msg.type == WSMsgType.TEXT: await client.send_str(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    if serial or binary_to_text: await client.send_str(msg.data.decode("utf-8", "replace"))
                    else: await client.send_bytes(msg.data)
                elif msg.type == WSMsgType.PING: await client.ping()
                elif msg.type == WSMsgType.PONG: await client.pong()
                elif msg.type == WSMsgType.CLOSE: await client.close(); break
        tasks=[asyncio.create_task(c2u()), asyncio.create_task(u2c())]; done,pending=await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending: t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        log.info("ws_closed", client_close_code=client.close_code, upstream_close_code=up.close_code, duration_ms=int((time.monotonic()-started)*1000))
        if not client.closed: await client.close()
        return client
    def _headers(self, request, upstream_url, websocket, cookie_header):
        p=__import__('urllib.parse').parse.urlsplit(upstream_url); skip={"connection","keep-alive","proxy-authenticate","proxy-authorization","te","trailers","transfer-encoding","upgrade","host","cookie","origin","referer","sec-websocket-key","sec-websocket-version","sec-websocket-extensions","sec-websocket-accept","sec-websocket-protocol"}
        h={k:v for k,v in request.headers.items() if k.lower() not in skip}; h["Host"]=p.netloc; h["Origin"]=self.url_builder.upstream_origin(upstream_url)
        if cookie_header: h["Cookie"]=cookie_header
        return h