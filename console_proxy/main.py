from pathlib import Path

import redis.asyncio as redis
from aiohttp import ClientSession, DummyCookieJar, TCPConnector, web

from .config import Config
from .cookies import ProviderCookieService
from .handlers.health import HealthHandler
from .handlers.novnc import NoVncConsoleHandler
from .handlers.register import RegisterHandler
from .handlers.register_dispatcher import ConsoleRegisterDispatcher
from .handlers.serial import SerialConsoleHandler
from .logging_utils import request_id_middleware, setup_logging
from .ovh_ipmi.browser_session import OvhIpmiBrowserSession
from .ovh_ipmi.cookies import OvhIpmiCookieService
from .ovh_ipmi.handlers import OvhIpmiConsoleHandler, OvhIpmiRegisterHandler
from .ovh_ipmi.http_proxy import OvhIpmiHTTPProxy
from .ovh_ipmi.lease import OvhIpmiLease
from .ovh_ipmi.url_utils import OvhIpmiURLBuilder, OvhIpmiURLValidator
from .ovh_ipmi.websocket_bridge import OvhIpmiWebSocketBridge
from .proxy.http_proxy import HTTPProxyService
from .proxy.websocket_bridge import WebSocketBridge
from .security import ClaimService
from .storage import SessionStore
from .url_utils import ConsoleModeDetector, URLBuilder, URLValidator


async def on_startup(app: web.Application) -> None:
    app["redis"] = redis.from_url(app["config"].redis_url, decode_responses=True)
    await app["redis"].ping()


async def on_cleanup(app: web.Application) -> None:
    if "ovh_ipmi_client" in app:
        await app["ovh_ipmi_client"].close()
    await app["redis"].aclose()


async def init_services(app: web.Application) -> None:
    config = app["config"]
    redis_client = app["redis"]
    validator = URLValidator(config)
    builder = URLBuilder(config, validator)
    store = SessionStore(redis_client, config)
    claims = ClaimService(redis_client, config)
    cookies = ProviderCookieService(redis_client, config)
    websocket_bridge = WebSocketBridge(config, builder, cookies)
    http_proxy = HTTPProxyService(config, builder, cookies)

    ovh_validator = OvhIpmiURLValidator()
    ovh_builder = OvhIpmiURLBuilder(config.public_base_url, ovh_validator)
    ovh_cookies = OvhIpmiCookieService(redis_client, config)
    ovh_browser_session = OvhIpmiBrowserSession(redis_client, config)
    ovh_lease = OvhIpmiLease(redis_client, config)
    app["ovh_ipmi_client"] = ClientSession(
        timeout=config.http_timeout,
        cookie_jar=DummyCookieJar(),
        auto_decompress=False,
        connector=TCPConnector(limit=config.ovh_upstream_connection_limit),
    )
    ovh_http_proxy = OvhIpmiHTTPProxy(
        config,
        ovh_builder,
        ovh_cookies,
        ovh_lease,
        app["ovh_ipmi_client"],
        AsrockAuthAdapter(),
    )
    ovh_websocket_bridge = OvhIpmiWebSocketBridge(
        config, ovh_builder, ovh_cookies, ovh_lease, app["ovh_ipmi_client"]
    )
    ovh_register_handler = OvhIpmiRegisterHandler(
        config, ovh_validator, ovh_builder, store, ovh_cookies
    )

    app["register_handler"] = RegisterHandler(
        config, ConsoleModeDetector(), validator, builder, store, cookies
    )
    app["register_dispatcher"] = ConsoleRegisterDispatcher(
        app["register_handler"], ovh_register_handler
    )
    app["health_handler"] = HealthHandler()
    app["novnc_handler"] = NoVncConsoleHandler(
        store, validator, claims, http_proxy, websocket_bridge
    )
    app["serial_handler"] = SerialConsoleHandler(
        config, store, validator, claims, websocket_bridge
    )
    app["ovh_ipmi_handler"] = OvhIpmiConsoleHandler(
        store, ovh_validator, ovh_browser_session, ovh_http_proxy,
        ovh_websocket_bridge,
    )


def create_app() -> web.Application:
    setup_logging()
    config = Config()
    app = web.Application(
        client_max_size=10 * 1024 * 1024,
        middlewares=[request_id_middleware],
    )
    app["config"] = config

    async def startup(application: web.Application) -> None:
        await on_startup(application)
        await init_services(application)

    app.on_startup.append(startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_static(
        "/static/",
        path=str(Path(__file__).resolve().parent / "static"),
        name="static",
    )

    async def health(request: web.Request) -> web.Response:
        return await request.app["health_handler"].health(request)

    async def ready(request: web.Request) -> web.Response:
        return await request.app["health_handler"].ready(request)

    async def register(request: web.Request) -> web.Response:
        return await request.app["register_dispatcher"](request)

    async def serial_page(request: web.Request) -> web.Response:
        return await request.app["serial_handler"].page(request)

    async def serial_ws(request: web.Request) -> web.StreamResponse:
        return await request.app["serial_handler"].ws(request)

    async def novnc(request: web.Request) -> web.StreamResponse:
        return await request.app["novnc_handler"].handle(request)

    async def ovh_ipmi(request: web.Request) -> web.StreamResponse:
        return await request.app["ovh_ipmi_handler"].handle(request)

    async def ovh_ipmi_root(request: web.Request) -> web.StreamResponse:
        return await request.app["ovh_ipmi_handler"].handle_root(request)

    app.router.add_get("/health", health)
    app.router.add_get("/ready", ready)
    app.router.add_post("/api/console/register", register)
    app.router.add_get("/p/{token}/serial", serial_page)
    app.router.add_get("/p/{token}/serial/ws", serial_ws)
    app.router.add_route("*", "/ipmi/{token}/{tail:.*}", ovh_ipmi)
    app.router.add_route("*", "/ipmi/{token}", ovh_ipmi)
    app.router.add_route("*", "/p/{token}/{tail:.*}", novnc)
    app.router.add_route("*", "/p/{token}", novnc)
    app.router.add_route("*", "/{tail:.*}", ovh_ipmi_root)
    return app