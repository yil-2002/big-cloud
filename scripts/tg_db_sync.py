#!/usr/bin/env python3
"""SQLite bazani yopiq Telegram kanalida saqlash (tashqi Postgres'siz rejim).

Fayllarning o'zi baribir kanalda (arxiv) turadi; bazada faqat metadata
(foydalanuvchilar, papkalar, ``file_id`` lar, havolalar) bor va u kichik.
GitHub Actions runner'i har ~6 soatda yangilanadi va diski o'chadi, shuning
uchun bazaning siqilgan nusxasi kanalga **bitta pin qilingan xabar** sifatida
yuboriladi va keyingi ishga tushishda o'sha xabardan tiklanadi.

Buyruqlar::

    python scripts/tg_db_sync.py restore data/hugecloud.sqlite3
    python scripts/tg_db_sync.py backup  data/hugecloud.sqlite3

Env: ``BOT_TOKEN``, ``ARCHIVE_CHANNEL_ID`` (bot kanalda admin: xabar yuborish,
tahrirlash va pin qilish huquqlari bilan).

Xavfsizlik qoidalari (ma'lumot yo'qolmasligi uchun):

* ``restore`` tarmoq xatosida **xato bilan chiqadi** (kod 2) — bo'sh baza
  bilan ishga tushib, keyin eski nusxani ustidan yozib yubormaslik uchun;
* kanalda boshqa (bizniki bo'lmagan) xabar pin qilingan bo'lsa ham to'xtaydi;
* ``backup`` faqat baza o'zgargan bo'lsa yuboradi (sha256 taqqoslash) va
  mavjud xabarni tahrirlaydi — kanal to'lib ketmaydi.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import httpx

TAG = "#hugecloud_db"
FILE_NAME = "hugecloud-db.sqlite3.gz"
MAX_DOWNLOAD = 20 * 1024 * 1024  # Bot API getFile chegarasi
STATE = Path(os.getenv("DB_SYNC_STATE", "data/.tg_db_sync.json"))


class SyncError(RuntimeError):
    """Qayta urinishdan keyin ham bajarilmagan amal."""


def _api() -> tuple[str, str]:
    token = os.environ.get("BOT_TOKEN", "").strip()
    chat = os.environ.get("ARCHIVE_CHANNEL_ID", "").strip()
    if not token or not chat:
        raise SystemExit("BOT_TOKEN va ARCHIVE_CHANNEL_ID kerak")
    api = os.environ.get("TG_API_BASE", "https://api.telegram.org").rstrip("/")
    return f"{api}/bot{token}", chat


def _call(client: httpx.Client, method: str, *, data=None, files=None, attempts: int = 4) -> dict:
    base, _ = _api()
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            resp = client.post(f"{base}/{method}", data=data, files=files, timeout=60)
            body = resp.json()
            if body.get("ok"):
                return body["result"]
            retry = (body.get("parameters") or {}).get("retry_after")
            if retry:
                time.sleep(min(int(retry), 30))
                continue
            # Telegram aniq rad etdi (huquq yo'q, chat topilmadi) — qayta urinish foydasiz.
            raise SyncError(f"{method}: {body.get('description')}")
        except (httpx.HTTPError, ValueError) as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    raise SyncError(f"{method}: tarmoq xatosi ({last})")


def _is_ours(message: dict | None) -> bool:
    doc = (message or {}).get("document") or {}
    return doc.get("file_name") == FILE_NAME and TAG in ((message or {}).get("caption") or "")


def _save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state), encoding="utf-8")


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def restore(db_path: Path) -> int:
    _, chat = _api()
    with httpx.Client() as client:
        info = _call(client, "getChat", data={"chat_id": chat})
        pinned = info.get("pinned_message")
        if not pinned:
            print("Kanalda baza nusxasi yo'q — yangi (bo'sh) baza bilan boshlanadi.")
            _save_state({})
            return 0
        if not _is_ours(pinned):
            print(
                "::error::Kanalda boshqa xabar pin qilingan. Baza nusxasini topib bo'lmadi. "
                f"Kanaldagi '{FILE_NAME}' xabarini pin qiling yoki boshqa pinni olib tashlang.",
                file=sys.stderr,
            )
            return 2
        doc = pinned["document"]
        if int(doc.get("file_size") or 0) > MAX_DOWNLOAD:
            print("::error::Baza nusxasi 20 MB dan katta — Bot API yuklab bera olmaydi.", file=sys.stderr)
            return 2
        meta = _call(client, "getFile", data={"file_id": doc["file_id"]})
        base, _ = _api()
        url = base.replace("/bot", "/file/bot", 1) + "/" + meta["file_path"]
        raw = client.get(url, timeout=120)
        raw.raise_for_status()
        data = gzip.decompress(raw.content)
        if not data.startswith(b"SQLite format 3\x00"):
            raise SyncError("yuklangan fayl SQLite emas")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)
        db_path.write_bytes(data)
        _save_state({"message_id": pinned["message_id"], "sha256": hashlib.sha256(data).hexdigest()})
        print(f"Baza kanaldan tiklandi ({len(data) // 1024} KB, xabar #{pinned['message_id']}).")
        return 0


def _snapshot(db_path: Path) -> bytes:
    """Ilova yozayotgan paytda ham izchil nusxa (SQLite backup API, WAL bilan xavfsiz)."""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "snap.sqlite3"
        src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        return target.read_bytes()


def backup(db_path: Path) -> int:
    if not db_path.exists():
        print("Baza fayli yo'q — yuboriladigan narsa yo'q.")
        return 0
    data = _snapshot(db_path)
    digest = hashlib.sha256(data).hexdigest()
    state = _load_state()
    if state.get("sha256") == digest:
        print("Baza o'zgarmagan — yuborilmadi.")
        return 0
    packed = gzip.compress(data, compresslevel=9)
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    caption = f"{TAG} · {stamp} · {len(data) // 1024} KB\nBu xabarni o'chirmang va pindan olmang."
    _, chat = _api()
    with httpx.Client() as client:
        message_id = state.get("message_id")
        if message_id:
            media = {"type": "document", "media": "attach://file", "caption": caption}
            try:
                _call(
                    client,
                    "editMessageMedia",
                    data={"chat_id": chat, "message_id": message_id, "media": json.dumps(media)},
                    files={"file": (FILE_NAME, packed, "application/gzip")},
                )
                _save_state({"message_id": message_id, "sha256": digest})
                print(f"Baza nusxasi yangilandi (xabar #{message_id}).")
                return 0
            except SyncError as exc:
                print(f"Tahrirlab bo'lmadi ({exc}) — yangi xabar yuboriladi.")
        sent = _call(
            client,
            "sendDocument",
            data={"chat_id": chat, "caption": caption, "disable_notification": "true"},
            files={"document": (FILE_NAME, packed, "application/gzip")},
        )
        _call(
            client,
            "pinChatMessage",
            data={"chat_id": chat, "message_id": sent["message_id"], "disable_notification": "true"},
        )
        _save_state({"message_id": sent["message_id"], "sha256": digest})
        print(f"Baza nusxasi kanalga yuborildi va pin qilindi (xabar #{sent['message_id']}).")
        return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in {"restore", "backup"}:
        print(__doc__)
        return 64
    try:
        return (restore if argv[0] == "restore" else backup)(Path(argv[1]))
    except SyncError as exc:
        print(f"::error::Telegram bilan sinxronlash muvaffaqiyatsiz: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
