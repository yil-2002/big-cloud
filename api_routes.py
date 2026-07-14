"""
Bu faylni botingizning asosiy bot.py yoki alohida api.py fayliga qo'shing.
Aiohttp web server ishlatiladi (aiogram bilan birgalikda ishlaydi).
"""

import hmac
import hashlib
import json
import time
from urllib.parse import unquote, parse_qsl
from aiohttp import web
from bot import dp, bot  # sizning mavjud bot instance

# ─── TELEGRAM INIT DATA VERIFICATION ─────────────────────────────────────────
def verify_init_data(init_data: str, bot_token: str) -> dict | None:
    """Mini App dan kelgan initData ni tekshiradi."""
    try:
        vals = dict(parse_qsl(init_data, strict_parsing=True))
        check_hash = vals.pop("hash", "")
        
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(vals.items()))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        
        if not hmac.compare_digest(expected, check_hash):
            return None
        
        # Auth date ni tekshirish (1 soatdan eski bo'lmasin)
        auth_date = int(vals.get("auth_date", 0))
        if time.time() - auth_date > 3600:
            return None
        
        user_data = vals.get("user", "{}")
        return json.loads(unquote(user_data))
    except Exception:
        return None


def get_user_from_request(request: web.Request) -> int | None:
    """Request dan user_id ni oladi (initData yoki query param)."""
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    if init_data:
        from config import BOT_TOKEN
        user = verify_init_data(init_data, BOT_TOKEN)
        if user:
            return user.get("id")
    # Development uchun fallback
    return request.rel_url.query.get("user_id")


# ─── CORS MIDDLEWARE ──────────────────────────────────────────────────────────
@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        return web.Response(headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, X-Telegram-Init-Data",
        })
    response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


# ─── API HANDLERS ─────────────────────────────────────────────────────────────

async def api_get_files(request: web.Request):
    """GET /api/files?folder_id=&user_id=
    Papka va fayllar ro'yxatini qaytaradi."""
    user_id = get_user_from_request(request)
    if not user_id:
        return web.json_response({"error": "Unauthorized"}, status=401)
    
    folder_id = request.rel_url.query.get("folder_id") or None
    
    # ─── DB dan oling (sizning mavjud aiosqlite DB) ───────────────────────────
    from database import db  # sizning DB instance
    
    async with db.execute(
        "SELECT id, name, created_at FROM folders WHERE user_id=? AND parent_id IS ?",
        (user_id, folder_id)
    ) as cur:
        folder_rows = await cur.fetchall()
    
    async with db.execute(
        """SELECT id, file_name, file_size, file_type, message_id, created_at
           FROM files WHERE user_id=? AND folder_id IS ? AND in_trash=0
           ORDER BY created_at DESC""",
        (user_id, folder_id)
    ) as cur:
        file_rows = await cur.fetchall()
    
    folders = [{"id": r[0], "name": r[1], "type": "folder", "date": r[2]} for r in folder_rows]
    
    files = []
    for r in file_rows:
        file_id, name, size, mime, msg_id, date = r
        # Telegram fayl URL ni olish
        try:
            tg_file = await bot.get_file(file_id)
            url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{tg_file.file_path}"
        except Exception:
            url = None
        
        files.append({
            "id": file_id,
            "name": name,
            "size": size,
            "mime": mime,
            "url": url,
            "date": date,
            "type": "file",
        })
    
    return web.json_response({"folders": folders, "files": files})


async def api_delete_file(request: web.Request):
    """DELETE /api/files/{file_id}"""
    user_id = get_user_from_request(request)
    if not user_id:
        return web.json_response({"error": "Unauthorized"}, status=401)
    
    file_id = request.match_info["file_id"]
    from database import db
    
    # Trash ga ko'chirish (to'g'ridan-to'g'ri o'chirish emas)
    await db.execute(
        "UPDATE files SET in_trash=1 WHERE id=? AND user_id=?",
        (file_id, user_id)
    )
    await db.commit()
    return web.json_response({"ok": True})


async def api_get_storage(request: web.Request):
    """GET /api/storage — Foydalanuvchi storage statistikasi."""
    user_id = get_user_from_request(request)
    if not user_id:
        return web.json_response({"error": "Unauthorized"}, status=401)
    
    from database import db
    
    async with db.execute(
        "SELECT COALESCE(SUM(file_size), 0) FROM files WHERE user_id=? AND in_trash=0",
        (user_id,)
    ) as cur:
        row = await cur.fetchone()
    
    used = row[0] if row else 0
    # Telegram botlar uchun cheklov yo'q, lekin ko'rsatish uchun limit qo'yamiz
    total = 50 * 1024 * 1024 * 1024  # 50 GB symbolic
    
    return web.json_response({"used": used, "total": total})


# ─── WEB APP YARATISH ─────────────────────────────────────────────────────────
def create_web_app():
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/api/files", api_get_files)
    app.router.add_delete("/api/files/{file_id}", api_delete_file)
    app.router.add_get("/api/storage", api_get_storage)
    return app


# ─── MAIN (aiogram + aiohttp birga ishlatish) ─────────────────────────────────
"""
main.py ga qo'shing:

import asyncio
from aiohttp import web
from aiohttp.web_runner import AppRunner, TCPSite

async def main():
    # Bot ni ishga tushirish
    bot_task = asyncio.create_task(dp.start_polling(bot))
    
    # Web server ni ishga tushirish
    web_app = create_web_app()
    runner = AppRunner(web_app)
    await runner.setup()
    site = TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    
    await bot_task

if __name__ == "__main__":
    asyncio.run(main())
"""
