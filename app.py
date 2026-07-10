import asyncio
import json
import os
import secrets
from http.cookies import CookieError, SimpleCookie
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from aiohttp import (
    ClientError,
    ClientSession,
    ClientTimeout,
    DummyCookieJar,
    WSMsgType,
    WSServerHandshakeError,
    web,
)
import redis.asyncio as redis


REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://vnc-zomro.com").rstrip("/")
REGISTER_API_TOKEN = os.getenv("REGISTER_API_TOKEN", "")
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "3000"))
REDIS_CLAIM_KEY_PREFIX = "console:claim:"
CLAIM_COOKIE_NAME = "console_proxy_claim"

# Якщо true, під час register сервіс сам робить GET на provider URL
# і забирає Set-Cookie від провайдера.
PREFETCH_PROVIDER_COOKIES = os.getenv("PREFETCH_PROVIDER_COOKIES", "true").lower() in (
    "1",
    "true",
    "yes",
)

ALLOWED_HOST_SUFFIXES = [
    suffix.strip().lower()
    for suffix in os.getenv(
        "ALLOWED_HOST_SUFFIXES",
        "cloud.gcore.com,ipmi.ovh.net",
    ).split(",")
    if suffix.strip()
]

REDIS_SESSION_KEY_PREFIX = "console:session:"
REDIS_COOKIE_KEY_PREFIX = "console:cookies:"

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

REQUEST_SKIP_HEADERS = HOP_BY_HOP_HEADERS | {
    "host",
    "cookie",
    "origin",
    "referer",
}

WS_SKIP_HEADERS = HOP_BY_HOP_HEADERS | {
    "host",
    "cookie",
    "origin",
    "referer",
    "sec-websocket-key",
    "sec-websocket-version",
    "sec-websocket-extensions",
    "sec-websocket-accept",
    "sec-websocket-protocol",
}

RESPONSE_SKIP_HEADERS = HOP_BY_HOP_HEADERS | {
    "set-cookie",
    # Щоб provider CSP не блокував websocket/assets уже на нашому домені.
    "content-security-policy",
    "content-security-policy-report-only",
    # На випадок якщо provider забороняє iframe/інший origin.
    "x-frame-options",
}

HTTP_TIMEOUT = ClientTimeout(total=None, sock_connect=30, sock_read=None)


def is_allowed_host(host: str) -> bool:
    if not host:
        return False

    host = host.lower().rstrip(".")

    for suffix in ALLOWED_HOST_SUFFIXES:
        suffix = suffix.lower().lstrip(".").rstrip(".")
        if host == suffix or host.endswith("." + suffix):
            return True

    return False


def validate_upstream_url(url: str) -> str:
    parsed = urlsplit(url)

    if parsed.scheme not in ("https", "http"):
        raise web.HTTPBadRequest(text="Only http/https upstream URLs are allowed")

    if not parsed.netloc or not parsed.hostname:
        raise web.HTTPBadRequest(text="Invalid upstream URL")

    if parsed.username or parsed.password:
        raise web.HTTPBadRequest(text="Credentials in upstream URL are not allowed")

    if not is_allowed_host(parsed.hostname):
        raise web.HTTPForbidden(text="Upstream host is not allowed")

    return url


def build_public_url(token: str, upstream_url: str) -> str:
    """
    Формує URL для клієнта на нашому домені.

    Для Gcore/noVNC важливо переписати query-параметр path.
    У значенні path НЕ ставимо початковий "/", бо noVNC часто сам
    додає "/" перед WebSocket path.

    Було у provider:
      /vnc_auto.html?path=%3Ftoken%3Dabc

    Стає у нас:
      /p/TOKEN/vnc_auto.html?path=p%2FTOKEN%2F%3Ftoken%3Dabc
    """
    parsed = urlsplit(upstream_url)

    upstream_path = parsed.path or "/"
    public_path = f"/p/{token}{upstream_path}"

    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    rewritten_pairs = []

    for key, value in query_pairs:
        if key == "path":
            if value.startswith("?"):
                value = f"p/{token}/" + value
            else:
                value = f"p/{token}/" + value.lstrip("/")

        rewritten_pairs.append((key, value))

    public_query = urlencode(rewritten_pairs)

    if public_query:
        return f"{PUBLIC_BASE_URL}{public_path}?{public_query}"

    return f"{PUBLIC_BASE_URL}{public_path}"


def strip_proxy_prefix(raw_path: str, token: str) -> str:
    prefix = f"/p/{token}"

    if not raw_path.startswith(prefix):
        raise web.HTTPBadRequest(text="Invalid proxy path")

    stripped = raw_path[len(prefix):]

    if not stripped:
        return "/"

    if not stripped.startswith("/"):
        return "/" + stripped

    return stripped


def make_upstream_url(
    upstream_url: str,
    request: web.Request,
    token: str,
    ws: bool = False,
) -> str:
    """
    Перетворює наш URL назад у provider URL.

    HTML entrypoint:
      /p/TOKEN/vnc_auto.html?path=p/TOKEN/?token=abc
    прокситься як:
      https://provider/vnc_auto.html?path=%3Ftoken%3Dabc

    WebSocket:
      /p/TOKEN/?token=abc
    прокситься як:
      wss://provider/?token=abc
    """
    parsed = urlsplit(upstream_url)

    upstream_path = strip_proxy_prefix(request.rel_url.raw_path, token)
    original_entry_path = parsed.path or "/"

    if not ws and upstream_path == original_entry_path:
        upstream_query = parsed.query
    else:
        upstream_query = request.rel_url.raw_query_string

    scheme = parsed.scheme
    if ws:
        scheme = "wss" if parsed.scheme == "https" else "ws"

    return urlunsplit((scheme, parsed.netloc, upstream_path, upstream_query, ""))


def upstream_origin(upstream_url: str) -> str:
    parsed = urlsplit(upstream_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def filter_request_headers(
    headers,
    upstream_url: str,
    websocket: bool = False,
    cookie_header: Optional[str] = None,
) -> dict:
    parsed = urlsplit(upstream_url)
    skip = WS_SKIP_HEADERS if websocket else REQUEST_SKIP_HEADERS

    result = {}
    for key, value in headers.items():
        if key.lower() not in skip:
            result[key] = value

    result["Host"] = parsed.netloc

    # Provider обычно ожидает Origin своего домена, а не vnc-zomro.com.
    # Для WebSocket это особенно важно.
    if websocket or headers.get("Origin"):
        result["Origin"] = upstream_origin(upstream_url)

    if headers.get("Referer"):
        result["Referer"] = upstream_url

    if cookie_header:
        result["Cookie"] = cookie_header

    return result


def rewrite_location_header(location: str, token: str, upstream_url: str) -> str:
    """
    Якщо provider повернув redirect на свій домен,
    переписуємо Location назад на наш домен.
    """
    absolute_location = urljoin(upstream_url, location)
    parsed = urlsplit(absolute_location)

    if parsed.hostname and is_allowed_host(parsed.hostname):
        return build_public_url(token, absolute_location)

    return location


def filter_response_headers(headers, token: str, upstream_url: str) -> dict:
    result = {}

    for key, value in headers.items():
        key_lower = key.lower()

        if key_lower in RESPONSE_SKIP_HEADERS:
            continue

        if key_lower == "location":
            value = rewrite_location_header(value, token, upstream_url)

        result[key] = value

    return result


async def save_provider_cookies(
    app: web.Application,
    token: str,
    set_cookie_headers: list[str],
) -> None:
    """
    Зберігаємо provider cookies у Redis:
      console:cookies:{token} = JSON dict
    """
    if not set_cookie_headers:
        return

    r = app["redis"]
    key = f"{REDIS_COOKIE_KEY_PREFIX}{token}"

    raw = await r.get(key)
    cookie_jar = {}

    if raw:
        try:
            cookie_jar = json.loads(raw)
        except Exception:
            cookie_jar = {}

    for header in set_cookie_headers:
        cookie = SimpleCookie()

        try:
            cookie.load(header)
        except CookieError:
            continue

        for name, morsel in cookie.items():
            if morsel["max-age"] == "0":
                cookie_jar.pop(name, None)
                continue

            cookie_jar[name] = morsel.value

    if cookie_jar:
        await r.setex(
            key,
            SESSION_TTL_SECONDS,
            json.dumps(cookie_jar),
        )
    else:
        await r.delete(key)


async def ensure_token_claimed(request: web.Request, token: str) -> tuple[bool, str | None]:
    """
    One-time логика:
    - первый браузер, который открыл /p/{token}/..., получает claim;
    - все следующие запросы должны иметь cookie с этим claim;
    - другой браузер по той же ссылке получает 403.

    Возвращает:
    - should_set_cookie
    - claim_id
    """
    r = request.app["redis"]

    claim_key = f"{REDIS_CLAIM_KEY_PREFIX}{token}"
    client_claim = request.cookies.get(CLAIM_COOKIE_NAME)

    stored_claim = await r.get(claim_key)

    # Ссылка уже захвачена.
    if stored_claim:
        if client_claim and client_claim == stored_claim:
            return False, stored_claim

        raise web.HTTPForbidden(text="Console link has already been used")

    # Ссылка ещё не захвачена. Пробуем захватить атомарно.
    new_claim = secrets.token_urlsafe(24)

    claimed = await r.set(
        claim_key,
        new_claim,
        ex=SESSION_TTL_SECONDS,
        nx=True,
    )

    if claimed:
        return True, new_claim

    # Race condition: кто-то захватил между GET и SET.
    stored_claim = await r.get(claim_key)

    if client_claim and stored_claim and client_claim == stored_claim:
        return False, stored_claim

    raise web.HTTPForbidden(text="Console link has already been used")


async def build_provider_cookie_header(
    app: web.Application,
    token: str,
    client_cookie_header: Optional[str],
) -> Optional[str]:
    """
    Збирає Cookie header для provider:
      - cookies, збережені в Redis;
      - cookies, які браузер прислав на vnc-zomro.com.
    """
    r = app["redis"]
    key = f"{REDIS_COOKIE_KEY_PREFIX}{token}"

    cookie_jar = {}

    raw = await r.get(key)
    if raw:
        try:
            cookie_jar.update(json.loads(raw))
        except Exception:
            pass

    if client_cookie_header:
        client_cookie = SimpleCookie()

        try:
            client_cookie.load(client_cookie_header)
            for name, morsel in client_cookie.items():
                cookie_jar[name] = morsel.value
        except CookieError:
            pass

    if not cookie_jar:
        return None

    return "; ".join(f"{name}={value}" for name, value in cookie_jar.items())


def rewrite_set_cookie_headers_for_client(
    set_cookie_headers: list[str],
    token: str,
) -> list[str]:
    """
    Переписує provider Set-Cookie так, щоб браузер прийняв cookie
    для vnc-zomro.com і відправляв їх тільки в межах /p/{token}/.
    """
    rewritten_headers = []

    for header in set_cookie_headers:
        cookie = SimpleCookie()

        try:
            cookie.load(header)
        except CookieError:
            continue

        for name, morsel in cookie.items():
            parts = [
                f"{name}={morsel.coded_value}",
                f"Path=/p/{token}/",
                "Secure",
                "SameSite=None",
            ]

            if morsel["max-age"]:
                parts.append(f"Max-Age={morsel['max-age']}")

            if morsel["expires"]:
                parts.append(f"Expires={morsel['expires']}")

            if morsel["httponly"]:
                parts.append("HttpOnly")

            rewritten_headers.append("; ".join(parts))

    return rewritten_headers


async def get_stored_cookies_for_client(
    app: web.Application,
    token: str,
) -> list[str]:
    """
    Якщо cookies уже були збережені під час prefetch/register,
    віддаємо їх браузеру клієнта як cookies для vnc-zomro.com.
    """
    r = app["redis"]
    raw = await r.get(f"{REDIS_COOKIE_KEY_PREFIX}{token}")

    if not raw:
        return []

    try:
        cookie_jar = json.loads(raw)
    except Exception:
        return []

    result = []

    for name, value in cookie_jar.items():
        cookie = SimpleCookie()
        cookie[name] = value
        cookie[name]["path"] = f"/p/{token}/"
        cookie[name]["secure"] = True
        cookie[name]["httponly"] = True
        cookie[name]["samesite"] = "None"

        result.append(cookie.output(header="").strip())

    return result


async def prefetch_provider_cookies(
    app: web.Application,
    token: str,
    upstream_url: str,
) -> None:
    """
    Робить GET на provider URL під час register,
    щоб забрати Set-Cookie саме з provider URL.
    """
    if not PREFETCH_PROVIDER_COOKIES:
        return

    parsed = urlsplit(upstream_url)

    headers = {
        "Host": parsed.netloc,
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        async with ClientSession(
            timeout=HTTP_TIMEOUT,
            cookie_jar=DummyCookieJar(),
            auto_decompress=False,
        ) as session:
            async with session.get(
                upstream_url,
                headers=headers,
                allow_redirects=False,
            ) as resp:
                set_cookie_headers = resp.headers.getall("Set-Cookie", [])
                await save_provider_cookies(app, token, set_cookie_headers)
                await resp.read()

                print(
                    f"prefetch token={token} status={resp.status} "
                    f"cookies={len(set_cookie_headers)}"
                )

    except Exception as exc:
        # Не валим register, если provider не отдал cookies.
        print(f"prefetch failed token={token}: {repr(exc)}")


async def get_redis(app: web.Application):
    return app["redis"]


async def register_console(request: web.Request) -> web.Response:
    if REGISTER_API_TOKEN:
        token_header = request.headers.get("X-Proxy-Token")
        if token_header != REGISTER_API_TOKEN:
            raise web.HTTPUnauthorized(text="Invalid proxy token")

    try:
        payload = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="Invalid JSON body")

    upstream_url = payload.get("upstream_url") or payload.get("url")
    if not upstream_url:
        raise web.HTTPBadRequest(text="upstream_url is required")

    upstream_url = validate_upstream_url(upstream_url)

    token = secrets.token_urlsafe(24)

    r = await get_redis(request.app)
    await r.setex(
        f"{REDIS_SESSION_KEY_PREFIX}{token}",
        SESSION_TTL_SECONDS,
        upstream_url,
    )

    await prefetch_provider_cookies(request.app, token, upstream_url)

    public_url = build_public_url(token, upstream_url)

    return web.json_response(
        {
            "token": token,
            "url": public_url,
            "ttl": SESSION_TTL_SECONDS,
        }
    )


async def get_upstream_from_token(request: web.Request, token: str) -> str:
    r = await get_redis(request.app)
    upstream_url = await r.get(f"{REDIS_SESSION_KEY_PREFIX}{token}")

    if not upstream_url:
        raise web.HTTPGone(text="Console session expired or not found")

    validate_upstream_url(upstream_url)
    return upstream_url

async def proxy_websocket(request: web.Request, token: str) -> web.WebSocketResponse:
    upstream_url = await get_upstream_from_token(request, token)

    await ensure_token_claimed(request, token)

    cookie_header = await build_provider_cookie_header(
        request.app,
        token,
        request.headers.get("Cookie"),
    )

    target_url = make_upstream_url(upstream_url, request, token, ws=True)

    headers = filter_request_headers(
        request.headers,
        upstream_url,
        websocket=True,
        cookie_header=cookie_header,
    )

    print(f"ws proxy token={token} target={target_url}")

    try:
        async with ClientSession(
            timeout=HTTP_TIMEOUT,
            cookie_jar=DummyCookieJar(),
        ) as session:
            async with session.ws_connect(
                target_url,
                headers=headers,
                heartbeat=30,
                max_msg_size=0,
                autoclose=True,
                autoping=True,
                protocols=("binary",),
            ) as upstream_ws:
                set_cookie_headers = upstream_ws._response.headers.getall("Set-Cookie", [])

                await save_provider_cookies(
                    request.app,
                    token,
                    set_cookie_headers,
                )

                client_ws = web.WebSocketResponse(
                    heartbeat=30,
                    autoping=True,
                    autoclose=True,
                    protocols=("binary",),
                )

                for cookie_header_to_client in await get_stored_cookies_for_client(
                    request.app,
                    token,
                ):
                    client_ws.headers.add("Set-Cookie", cookie_header_to_client)

                await client_ws.prepare(request)

                async def client_to_upstream():
                    async for msg in client_ws:
                        if msg.type == WSMsgType.TEXT:
                            await upstream_ws.send_str(msg.data)
                        elif msg.type == WSMsgType.BINARY:
                            await upstream_ws.send_bytes(msg.data)
                        elif msg.type == WSMsgType.PING:
                            await upstream_ws.ping()
                        elif msg.type == WSMsgType.PONG:
                            await upstream_ws.pong()
                        elif msg.type == WSMsgType.CLOSE:
                            await upstream_ws.close()
                            break

                async def upstream_to_client():
                    async for msg in upstream_ws:
                        if msg.type == WSMsgType.TEXT:
                            await client_ws.send_str(msg.data)
                        elif msg.type == WSMsgType.BINARY:
                            await client_ws.send_bytes(msg.data)
                        elif msg.type == WSMsgType.PING:
                            await client_ws.ping()
                        elif msg.type == WSMsgType.PONG:
                            await client_ws.pong()
                        elif msg.type == WSMsgType.CLOSE:
                            await client_ws.close()
                            break

                tasks = [
                    asyncio.create_task(client_to_upstream()),
                    asyncio.create_task(upstream_to_client()),
                ]

                done, pending = await asyncio.wait(
                    tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                for task in pending:
                    task.cancel()

                await asyncio.gather(*pending, return_exceptions=True)

                if not client_ws.closed:
                    await client_ws.close()

                return client_ws

    except WSServerHandshakeError as exc:
        print(
            f"ws handshake failed token={token} "
            f"status={exc.status} message={exc.message} target={target_url}"
        )
        raise web.HTTPBadGateway(
            text=f"Upstream WebSocket handshake failed: {exc.status}"
        )

    except ClientError as exc:
        print(f"ws client error token={token}: {repr(exc)} target={target_url}")
        raise web.HTTPBadGateway(text="Upstream WebSocket connection failed")

    except Exception as exc:
        print(f"ws unexpected error token={token}: {repr(exc)} target={target_url}")
        raise web.HTTPBadGateway(text="Unexpected WebSocket proxy error")

async def proxy_http(request: web.Request) -> web.StreamResponse:
    token = request.match_info["token"]

    should_set_claim_cookie, claim_id = await ensure_token_claimed(request, token)

    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await proxy_websocket(request, token)

    upstream_url = await get_upstream_from_token(request, token)

    target_url = make_upstream_url(upstream_url, request, token, ws=False)

    cookie_header = await build_provider_cookie_header(
        request.app,
        token,
        request.headers.get("Cookie"),
    )

    headers = filter_request_headers(
        request.headers,
        upstream_url,
        websocket=False,
        cookie_header=cookie_header,
    )

    data = request.content.iter_chunked(65536) if request.can_read_body else None

    print(f"http proxy token={token} target={target_url}")

    try:
        async with ClientSession(
            timeout=HTTP_TIMEOUT,
            cookie_jar=DummyCookieJar(),
            auto_decompress=False,
        ) as session:
            async with session.request(
                method=request.method,
                url=target_url,
                headers=headers,
                data=data,
                allow_redirects=False,
            ) as upstream_response:
                set_cookie_headers = upstream_response.headers.getall("Set-Cookie", [])

                await save_provider_cookies(
                    request.app,
                    token,
                    set_cookie_headers,
                )

                response_headers = filter_response_headers(
                    upstream_response.headers,
                    token,
                    upstream_url,
                )

                response = web.StreamResponse(
                    status=upstream_response.status,
                    reason=upstream_response.reason,
                    headers=response_headers,
                )

                # Cookies, которые пришли именно в текущем upstream response.
                for cookie_header_to_client in rewrite_set_cookie_headers_for_client(
                    set_cookie_headers,
                    token,
                ):
                    response.headers.add("Set-Cookie", cookie_header_to_client)

                # Cookies, которые уже лежали в Redis после prefetch/register.
                for cookie_header_to_client in await get_stored_cookies_for_client(
                    request.app,
                    token,
                ):
                    response.headers.add("Set-Cookie", cookie_header_to_client)

                if should_set_claim_cookie and claim_id:
                    response.set_cookie(
                    CLAIM_COOKIE_NAME,
                    claim_id,
                    path=f"/p/{token}/",
                    max_age=SESSION_TTL_SECONDS,
                    secure=True,
                    httponly=True,
                    samesite="None",
                )

                await response.prepare(request)

                async for chunk in upstream_response.content.iter_chunked(65536):
                    await response.write(chunk)

                await response.write_eof()
                return response

    except ClientError as exc:
        print(f"http client error token={token}: {repr(exc)} target={target_url}")
        raise web.HTTPBadGateway(text="Upstream HTTP request failed")

    except Exception as exc:
        print(f"http unexpected error token={token}: {repr(exc)} target={target_url}")
        raise web.HTTPBadGateway(text="Unexpected HTTP proxy error")


async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def on_startup(app: web.Application):
    app["redis"] = redis.from_url(REDIS_URL, decode_responses=True)
    await app["redis"].ping()


async def on_cleanup(app: web.Application):
    await app["redis"].close()


def create_app() -> web.Application:
    app = web.Application(client_max_size=10 * 1024 * 1024)

    app.router.add_get("/health", health)
    app.router.add_post("/api/console/register", register_console)

    app.router.add_route("*", "/p/{token}/{tail:.*}", proxy_http)
    app.router.add_route("*", "/p/{token}", proxy_http)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="127.0.0.1", port=5000)