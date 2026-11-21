"""API routers, exported for :func:`api.app.create_app`."""

from api.routes.auth import router as auth_router
from api.routes.files import router as files_router
from api.routes.folders import router as folders_router
from api.routes.shares import router as shares_router
from api.routes.stream import router as stream_router
from api.routes.system import router as system_router

__all__ = [
    "auth_router",
    "files_router",
    "folders_router",
    "shares_router",
    "stream_router",
    "system_router",
]
