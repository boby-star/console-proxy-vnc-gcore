import os

from aiohttp import web

from .logging_utils import SafeAccessLogger
from .main import create_app


if __name__ == "__main__":
    web.run_app(
        create_app(),
        host=os.getenv("APP_HOST", "0.0.0.0"),
        port=int(os.getenv("APP_PORT", "5000")),
        access_log_class=SafeAccessLogger,
    )