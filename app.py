from console_proxy.main import create_app
from aiohttp import web

if __name__ == "__main__":
    web.run_app(create_app(), host="127.0.0.1", port=5000)