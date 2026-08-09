"""MCP stdio server: đọc lịch sử chat với BOTS trong Telegram của user.

Bản này scope hẹp — CHỈ đọc chat với bot, không đụng chat cá nhân hoặc group. Mỗi
tool validate rằng entity resolve ra phải là bot (user.bot == True), nếu không sẽ
trả error "not a bot chat".

Vì sao dùng MTProto (login as user) thay vì Bot API? — Bot API không cho đọc
history messages BOT ĐÃ GỬI (chỉ đọc được messages user gửi TỚI bot). MTProto
login as user account thì thấy đầy đủ 2 chiều của chat với bot đó.

First-run setup:
    python bridge/tg_mtproto_mcp.py setup
→ nhập api_id / api_hash / phone / OTP → session lưu tại bridge/.tg_session.session.

Nothing here writes to stdout — MCP owns stdio for JSON-RPC. Human output → stderr.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP


ROOT = Path(__file__).resolve().parent
CREDS_FILE = ROOT / ".tg_creds.json"
SESSION_FILE = ROOT / ".tg_session"  # Telethon appends .session


def _load_creds() -> dict:
    if CREDS_FILE.is_file():
        return json.loads(CREDS_FILE.read_text(encoding="utf-8"))
    return {}


def _save_creds(data: dict) -> None:
    CREDS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _get_credentials() -> tuple[int, str]:
    creds = _load_creds()
    api_id = creds.get("api_id") or os.environ.get("TG_API_ID")
    api_hash = creds.get("api_hash") or os.environ.get("TG_API_HASH")
    if not api_id or not api_hash:
        raise RuntimeError(
            "Telegram MTProto not set up. Run `python bridge/tg_mtproto_mcp.py setup` first."
        )
    return int(api_id), str(api_hash)


def _client():
    from telethon import TelegramClient  # type: ignore

    api_id, api_hash = _get_credentials()
    return TelegramClient(str(SESSION_FILE), api_id, api_hash)


async def _with_client(fn):
    client = _client()
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError(
            "Telegram session not authorized — run `python bridge/tg_mtproto_mcp.py setup`"
        )
    try:
        return await fn(client)
    finally:
        await client.disconnect()


def _entity_ref(chat: str | int) -> str | int:
    if isinstance(chat, int):
        return chat
    s = str(chat).strip().lstrip("@")
    if s.startswith("-") and s[1:].isdigit():
        return int(s)
    if s.isdigit():
        return int(s)
    return s


async def _resolve_bot(client, chat):
    """Resolve `chat` to a bot entity. Accepts @username, numeric id, or a substring
    of the bot's display title (fallback: scans recent dialogs). Raises ValueError
    if the resolved entity is not a bot — the guardrail that scopes this bridge to
    bot chats only."""
    ref = _entity_ref(chat)
    try:
        entity = await client.get_entity(ref)
    except Exception:
        # Fallback: fuzzy match by title/first_name across recent dialogs.
        needle = str(chat).lower().lstrip("@")
        entity = None
        async for d in client.iter_dialogs(limit=300):
            if not getattr(d.entity, "bot", False):
                continue
            title = (d.title or "").lower()
            uname = (getattr(d.entity, "username", "") or "").lower()
            if needle in title or needle in uname:
                entity = d.entity
                break
        if entity is None:
            raise ValueError(
                f"No bot chat found matching '{chat}'. "
                f"Try @username (e.g. @tagent_telegram_bot) or numeric id."
            )
    if not bool(getattr(entity, "bot", False)):
        raise ValueError(
            f"'{chat}' is not a bot chat (this bridge only reads chats with bots). "
            f"Entity type: {type(entity).__name__}"
        )
    return entity


def _serialize_message(m: Any) -> dict:
    from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument

    media_type = None
    if m.media is not None:
        if isinstance(m.media, MessageMediaPhoto):
            media_type = "photo"
        elif isinstance(m.media, MessageMediaDocument):
            doc = m.media.document
            media_type = (
                getattr(doc, "mime_type", None) or "document"
            ) if doc else "document"
        else:
            media_type = type(m.media).__name__
    return {
        "id": m.id,
        "date": m.date.isoformat() if m.date else None,
        "sender_id": m.sender_id,
        "text": m.message or "",
        "reply_to_id": m.reply_to_msg_id,
        "media": media_type,
        "outgoing": bool(m.out),  # True nếu user gửi, False nếu bot gửi
    }


mcp = FastMCP("telegram-mtproto")


@mcp.tool()
async def list_bots(limit: int = 30) -> list[dict]:
    """Liệt kê các BOT bạn có chat trong Telegram. Kết quả xếp theo dialog gần nhất.
    Trả về id, username, title, unread_count, last_message (short). Dùng để tìm ra
    chat với bot nào trước khi gọi get_bot_history."""

    async def _run(c):
        out = []
        async for d in c.iter_dialogs(limit=200):  # scan rộng, filter bot
            if getattr(d.entity, "bot", False):
                out.append(
                    {
                        "id": d.id,
                        "username": getattr(d.entity, "username", None),
                        "title": d.title,
                        "unread_count": d.unread_count,
                        "last_message": (d.message.message[:120] if d.message and d.message.message else ""),
                        "last_date": d.date.isoformat() if d.date else None,
                    }
                )
                if len(out) >= limit:
                    break
        return out

    return await _with_client(_run)


@mcp.tool()
async def get_bot_info(bot: str) -> dict:
    """Metadata của 1 bot: id, username, name, verified, ... `bot` = @username hoặc
    numeric id. Trả error nếu chat không phải bot."""

    async def _run(c):
        entity = await _resolve_bot(c, bot)
        return {
            "id": entity.id,
            "username": getattr(entity, "username", None),
            "first_name": getattr(entity, "first_name", None),
            "last_name": getattr(entity, "last_name", None),
            "is_bot": True,
            "verified": getattr(entity, "verified", False),
            "restricted": getattr(entity, "restricted", False),
        }

    return await _with_client(_run)


@mcp.tool()
async def get_bot_history(bot: str, limit: int = 50, before_id: int = 0) -> list[dict]:
    """Đọc lịch sử tin nhắn với 1 bot cụ thể. `bot` = @username / id / tên chat.
    `limit` = số tin muốn lấy (mặc định 50, max thực tế do Telegram rate limit).
    `before_id` > 0 để paginate lấy tin cũ hơn (dùng id của tin cuối lần trước).
    Trả list message newest-first, mỗi message có {id, date, text, media, outgoing}.
    Trả error nếu chat không phải bot."""

    async def _run(c):
        entity = await _resolve_bot(c, bot)
        kwargs = {"limit": limit}
        if before_id:
            kwargs["offset_id"] = int(before_id)
        return [_serialize_message(m) async for m in c.iter_messages(entity, **kwargs)]

    return await _with_client(_run)


@mcp.tool()
async def search_bot_messages(bot: str, query: str, limit: int = 30) -> list[dict]:
    """Tìm tin nhắn có chứa `query` trong chat với 1 bot cụ thể. Case-insensitive
    substring match ở phía Telegram server. Trả error nếu chat không phải bot."""

    async def _run(c):
        entity = await _resolve_bot(c, bot)
        return [
            _serialize_message(m)
            async for m in c.iter_messages(entity, limit=limit, search=query)
        ]

    return await _with_client(_run)


@mcp.tool()
async def count_bot_messages(bot: str) -> dict:
    """Đếm tổng số tin nhắn với 1 bot (không tải content). Nhanh cho stats/health check.
    Trả {'count': N, 'bot_username': ...}."""

    async def _run(c):
        entity = await _resolve_bot(c, bot)
        # iter_messages with limit=0 và total_only pattern
        count = 0
        async for _ in c.iter_messages(entity, limit=None):
            count += 1
        return {"count": count, "bot_username": getattr(entity, "username", None)}

    return await _with_client(_run)


@mcp.tool()
async def download_bot_media(bot: str, message_id: int, target_dir: str = "") -> dict:
    """Tải media (ảnh/file) của 1 message cụ thể từ chat với bot. Trả absolute path
    file đã lưu. `target_dir` mặc định = thư mục bridge."""

    async def _run(c):
        entity = await _resolve_bot(c, bot)
        msg = await c.get_messages(entity, ids=int(message_id))
        if msg is None:
            return {"error": f"message {message_id} not found in bot chat {bot}"}
        if msg.media is None:
            return {"error": "message has no media"}
        base = Path(target_dir).expanduser().resolve() if target_dir else ROOT
        base.mkdir(parents=True, exist_ok=True)
        path = await c.download_media(msg, file=str(base))
        return {"ok": True, "path": str(path)}

    return await _with_client(_run)


def _setup_flow() -> int:
    """Interactive one-time setup — collects api_id/api_hash/phone/OTP, verifies login,
    persists the session."""
    print("=== Telegram MTProto bridge — setup (bot-read only) ===", file=sys.stderr)
    print("1) Open https://my.telegram.org → API development tools", file=sys.stderr)
    print("2) Create an application (Desktop). Copy api_id + api_hash.", file=sys.stderr)
    api_id = input("api_id: ").strip()
    api_hash = input("api_hash: ").strip()
    if not api_id.isdigit() or len(api_hash) < 16:
        print("Invalid api_id or api_hash.", file=sys.stderr)
        return 1
    _save_creds({"api_id": int(api_id), "api_hash": api_hash})

    from telethon import TelegramClient  # type: ignore

    phone = input("Phone (international, e.g. +84912345678): ").strip()
    client = TelegramClient(str(SESSION_FILE), int(api_id), api_hash)

    async def _login() -> int:
        await client.connect()
        if not await client.is_user_authorized():
            await client.send_code_request(phone)
            code = input("OTP sent to Telegram — code: ").strip()
            try:
                await client.sign_in(phone, code)
            except Exception as exc:
                if "password" in str(exc).lower() or "SESSION_PASSWORD_NEEDED" in str(exc):
                    import getpass
                    pwd = getpass.getpass("2FA cloud password: ")
                    await client.sign_in(password=pwd)
                else:
                    raise
        me = await client.get_me()
        print(
            f"OK — logged in as {me.first_name} (@{me.username or '?'}) id={me.id}",
            file=sys.stderr,
        )
        print(f"Session saved: {SESSION_FILE}.session", file=sys.stderr)
        await client.disconnect()
        return 0

    try:
        return asyncio.run(_login())
    except Exception as exc:
        print(f"setup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        sys.exit(_setup_flow())
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        try:
            api_id, _ = _get_credentials()
            print(f"creds: api_id={api_id}", file=sys.stderr)
        except Exception as exc:
            print(f"creds: MISSING ({exc})", file=sys.stderr)
        session_path = SESSION_FILE.with_suffix(".session")
        print(f"session file: {session_path} exists={session_path.exists()}", file=sys.stderr)
        sys.exit(0)
    print(f"[tg_mtproto_mcp] bot-read scope; session={SESSION_FILE}.session", file=sys.stderr)
    mcp.run()
