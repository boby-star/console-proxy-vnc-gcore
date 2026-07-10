import redis.asyncio as redis
from aiohttp import web
from .config import Config
from .logging_utils import setup_logging, request_id_middleware
from .url_utils import ConsoleModeDetector, URLValidator, URLBuilder
from .storage import SessionStore
from .security import ClaimService
from .cookies import ProviderCookieService
from .proxy.http_proxy import HTTPProxyService
from .proxy.websocket_bridge import WebSocketBridge
from .handlers.register import RegisterHandler
from .handlers.health import HealthHandler
from .handlers.novnc import NoVncConsoleHandler
from .handlers.serial import SerialConsoleHandler

async def on_startup(app):
    app["redis"] = redis.from_url(app["config"].redis_url, decode_responses=True); await app["redis"].ping()
async def on_cleanup(app): await app["redis"].close()

def create_app():
    setup_logging(); config=Config(); app=web.Application(client_max_size=10*1024*1024, middlewares=[request_id_middleware]); app["config"]=config
    async def init_services(app):
        r=app["redis"]; validator=URLValidator(config); builder=URLBuilder(config, validator); store=SessionStore(r, config); claims=ClaimService(r, config); cookies=ProviderCookieService(r, config); ws=WebSocketBridge(config, builder, cookies); hp=HTTPProxyService(config, builder, cookies)
        app["register_handler"]=RegisterHandler(config, ConsoleModeDetector(), validator, builder, store, cookies); app["health_handler"]=HealthHandler(); app["novnc_handler"]=NoVncConsoleHandler(store, validator, claims, hp, ws); app["serial_handler"]=SerialConsoleHandler(config, store, validator, claims, ws)
    async def startup(app): await on_startup(app); await init_services(app)
    app.on_startup.append(startup); app.on_cleanup.append(on_cleanup)
    app.router.add_static('/static/', path=str(__import__('pathlib').Path(__file__).resolve().parent/'static'), name='static')
    async def health(request): return await request.app['health_handler'].health(request)
    async def ready(request): return await request.app['health_handler'].ready(request)
    async def register(request): return await request.app['register_handler'](request)
    async def serial_page(request): return await request.app['serial_handler'].page(request)
    async def serial_ws(request): return await request.app['serial_handler'].ws(request)
    async def novnc(request): return await request.app['novnc_handler'].handle(request)
    app.router.add_get('/health', health); app.router.add_get('/ready', ready)
    app.router.add_post('/api/console/register', register)
    app.router.add_get('/p/{token}/serial', serial_page); app.router.add_get('/p/{token}/serial/ws', serial_ws)
    app.router.add_route('*','/p/{token}/{tail:.*}', novnc); app.router.add_route('*','/p/{token}', novnc)
    return app

if __name__ == '__main__': web.run_app(create_app(), host='127.0.0.1', port=5000)