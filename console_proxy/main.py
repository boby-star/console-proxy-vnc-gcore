from pathlib import Path

import redis.asyncio as redis
from aiohttp import web

from .config import Config
from .cookies import ProviderCookieService
from .handlers.health import HealthHandler
from .handlers.novnc import NoVncConsoleHandler
from .handlers.register import RegisterHandler
from .handlers.serial import SerialConsoleHandler
from .logging_utils import request_id_middleware, setup_logging
from .proxy.http_proxy import HTTPProxyService
from .proxy.websocket_bridge import WebSocketBridge
from .security import ClaimService
from .storage import SessionStore
from .url_utils import ConsoleModeDetector, URLBuilder, URLValidator


async def on_startup(app: web.Application) -> None:
    app["redis"] = redis.from_url(app["config"].redis_url, decode_responses=True)
    await app["redis"].ping()


async def on_cleanup(app: web.Application) -> None:
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

    app["register_handler"] = RegisterHandler(
        config, ConsoleModeDetector(), validator, builder, store, cookies
    )
    app["health_handler"] = HealthHandler()
    app["novnc_handler"] = NoVncConsoleHandler(
        store, validator, claims, http_proxy, websocket_bridge
    )
    app["serial_handler"] = SerialConsoleHandler(
        config, store, validator, claims, websocket_bridge
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
        return await request.app["register_handler"](request)

    async def serial_page(request: web.Request) -> web.Response:
        return await request.app["serial_handler"].page(request)

    async def serial_ws(request: web.Request) -> web.StreamResponse:
        return await request.app["serial_handler"].ws(request)

    async def novnc(request: web.Request) -> web.StreamResponse:
        return await request.app["novnc_handler"].handle(request)

    app.router.add_get("/health", health)
    app.router.add_get("/ready", ready)
    app.router.add_post("/api/console/register", register)
    app.router.add_get("/p/{token}/serial", serial_page)
    app.router.add_get("/p/{token}/serial/ws", serial_ws)
    app.router.add_route("*", "/p/{token}/{tail:.*}", novnc)
    app.router.add_route("*", "/p/{token}", novnc)
    return app