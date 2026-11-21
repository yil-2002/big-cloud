"""Bot routers, exported in registration order.

Registration priority lives in :func:`bot.setup.register_routers`; the imports
below only make the names available.
"""

from bot.handlers.admin import router as admin_router
from bot.handlers.auth import router as auth_router
from bot.handlers.browsing import router as browsing_router
from bot.handlers.callbacks import router as callbacks_router
from bot.handlers.commands import router as commands_router
from bot.handlers.files import router as files_router
from bot.handlers.inline import router as inline_router
from bot.handlers.miniapp import router as miniapp_router
from bot.handlers.share import router as share_router
from bot.handlers.start import router as start_router
from bot.handlers.storage import router as storage_router
from bot.handlers.upload import router as upload_router

__all__ = [
    "admin_router",
    "auth_router",
    "browsing_router",
    "callbacks_router",
    "commands_router",
    "files_router",
    "inline_router",
    "miniapp_router",
    "share_router",
    "start_router",
    "storage_router",
    "upload_router",
]
