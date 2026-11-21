import asyncio
import logging
from pathlib import Path
from collections.abc import AsyncIterator

from telethon import TelegramClient
from telethon.errors import FloodWaitError

from config import Settings

logger = logging.getLogger(__name__)

class MTProtoError(Exception):
    """Base exception for MTProto operations."""

_client: TelegramClient | None = None
_client_lock = asyncio.Lock()

async def get_client(settings: Settings) -> TelegramClient | None:
    """Lazy singleton to initialize and return the MTProto client."""
    global _client
    if not settings.mt.configured:
        return None

    async with _client_lock:
        if _client is not None:
            return _client

        try:
            client = TelegramClient(
                settings.mt.session_path,
                settings.mt.api_id,
                settings.mt.api_hash,
            )
            await client.start(bot_token=settings.bot_token)
            # Sessiya fayli bot hisobiga to'liq kirish beradi — faqat egasi
            # o'qiy olishi shart (aks holda lokal foydalanuvchi uni ko'chirib
            # olib, bot nomidan ish qilishi mumkin edi).
            for suffix in ("", "-journal"):
                path = Path(f"{settings.mt.session_path}{suffix}")
                if path.exists():
                    try:
                        path.chmod(0o600)
                    except OSError as exc:  # pragma: no cover - fayl tizimi
                        logger.warning("session fayli ruxsatini o'zgartirib bo'lmadi: %s", exc)
            _client = client
            return _client
        except Exception as e:
            logger.error("Failed to start MTProto client: %s", e)
            return None

async def iter_range(
    client: TelegramClient,
    chat_id: int | str,
    message_id: int,
    offset: int,
    limit: int,
    chunk: int = 1048576
) -> AsyncIterator[bytes]:
    """Iteratively download a file range using MTProto."""
    try:
        message = await client.get_messages(chat_id, ids=message_id)
    except Exception as e:
        raise MTProtoError(f"Failed to fetch message: {e}")

    if not message or getattr(message, "media", None) is None:
        raise MTProtoError("Message not found or has no media")

    # DIQQAT: Telethon'da ``limit`` — **bo'lak soni**, bayt emas (hujjat:
    # "The limit for how many *chunks* will be yielded at most"). Shu sababli
    # chegarani o'zimiz hisoblaymiz: aks holda Range so'rovi butun faylni
    # oqizib yuborardi (jonli o'lchov: 45 s da 85 MB).
    downloaded = 0
    while downloaded < limit:
        try:
            async for piece in client.iter_download(
                message.media,
                offset=offset + downloaded,
                request_size=chunk,
            ):
                remaining = limit - downloaded
                if remaining <= 0:
                    break
                if len(piece) > remaining:
                    piece = piece[:remaining]
                yield piece
                downloaded += len(piece)
                if downloaded >= limit:
                    break
            return
        except FloodWaitError as e:
            logger.warning("MTProto FloodWait: sleeping for %s seconds", e.seconds)
            await asyncio.sleep(e.seconds)
        except Exception as e:
            raise MTProtoError(f"Download failed: {e}")
