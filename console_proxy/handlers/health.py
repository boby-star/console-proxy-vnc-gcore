from aiohttp import web
class HealthHandler:
    async def health(self, request): return web.json_response({"status":"ok"})
    async def ready(self, request):
        try:
            await request.app["redis"].ping(); return web.json_response({"status":"ready"})
        except Exception:
            raise web.HTTPServiceUnavailable(text="Redis unavailable")