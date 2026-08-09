"""MCP stdio server exposing Telegram Bot API tools.

Uses the bot token OpenWorker already has stored under `telegram:default` in
`%APPDATA%\\coworker\\secrets.json` — no user setup needed. This is the "cheap
path": bot-scoped access only (bot can only see messages sent TO it), but
doesn't need the user's api_id/api_hash/phone/OTP dance that the MTProto
bridge requires.

Complements `send_message` which OpenWorker's built-in messaging connector
already exposes — this bridge adds `get_me`, `get_updates`, `get_chat`,
`send_photo`, `send_document`, `edit_message`, `delete_message`, and
`get_chat_member` for richer bot introspection.

Nothing here writes to stdout — MCP owns stdio for JSON-RPC.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

import httpx  # bundled with the runtime (via fastapi deps)

from mcp.server.fastmcp import FastMCP


def _state_dir() -> Path:
    """Match `coworker.secrets.state_dir()` — env override, then %APPDATA%\\coworker."""
    base = os.environ.get("COWORKER_STATE_DIR")
    if base:
        return Path(base).expanduser()
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "coworker"
    return Path.home() / ".config" / "coworker"


def _secrets_path() -> Path:
    return _state_dir() / "secrets.json"


def _read_secrets() -> dict:
    p = _secrets_path()
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_secrets(data: dict) -> None:
    p = _secrets_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _active_bot_name(data: dict = None) -> str:
    """Read the currently-active bot slot. Pointer lives at `telegram:_active`.
    Falls back to `default` when unset — keeps single-bot setups unchanged."""
    d = data if data is not None else _read_secrets()
    ptr = d.get("telegram:_active") or {}
    return str(ptr.get("name") or "default")


def _load_bot_token(account: str = "") -> str:
    """Pull the bot_token for `account` (or the currently-active slot when empty)
    from the coworker SecretStore. Errors surface via each tool's return value so
    agents can act on them."""
    data = _read_secrets()
    if not data:
        raise RuntimeError(f"secrets file not found: {_secrets_path()}")
    name = account or _active_bot_name(data)
    entry = data.get(f"telegram:{name}") or {}
    token = entry.get("bot_token")
    if not token:
        available = [k.split(":", 1)[1] for k in data if k.startswith("telegram:") and k != "telegram:_active"]
        raise RuntimeError(
            f"no bot_token for account '{name}'. Connected accounts: {available or '(none)'}. "
            "Use connect_bot(bot_token, account) to add one."
        )
    return str(token)


def _api(method: str, **params: Any) -> dict[str, Any]:
    """Call bot API. Returns the `result` payload on success, or `{'error': ...}` on
    failure. Never raises so the agent can iterate on errors."""
    try:
        token = _load_bot_token()
    except Exception as exc:
        return {"error": str(exc)}
    url = f"https://api.telegram.org/bot{token}/{method}"
    # Trim None params — Bot API rejects most explicit nulls.
    body = {k: v for k, v in params.items() if v is not None}
    try:
        r = httpx.post(url, json=body, timeout=30)
        data = r.json()
    except Exception as exc:
        return {"error": f"network: {exc}"}
    if not data.get("ok"):
        return {"error": data.get("description") or f"telegram api {method} failed"}
    return data.get("result", {})


def _api_upload(method: str, files: dict, **params: Any) -> dict[str, Any]:
    """Same as _api but uploads local files via multipart. `files` maps Bot API
    field name → local path (e.g. {"document": r"C:\\a\\b.zip"}). Streams the file
    so large uploads (up to Telegram's 50MB Bot API cap) don't blow memory."""
    try:
        token = _load_bot_token()
    except Exception as exc:
        return {"error": str(exc)}
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = {k: str(v) for k, v in params.items() if v is not None}
    opened = []
    try:
        multipart = {}
        for field, path in files.items():
            p = Path(path).expanduser().resolve()
            if not p.is_file():
                return {"error": f"file not found: {p}"}
            fh = p.open("rb")
            opened.append(fh)
            multipart[field] = (p.name, fh)
        try:
            r = httpx.post(url, data=data, files=multipart, timeout=300)
            resp = r.json()
        except Exception as exc:
            return {"error": f"network: {exc}"}
    finally:
        for fh in opened:
            try:
                fh.close()
            except Exception:
                pass
    if not resp.get("ok"):
        return {"error": resp.get("description") or f"telegram api {method} failed"}
    return resp.get("result", {})


def _is_local_file(s: str) -> bool:
    """True if `s` looks like a local filesystem path (not http/https/tg://…)."""
    if not s:
        return False
    low = s.lower()
    if low.startswith(("http://", "https://", "tg://", "attach://")):
        return False
    try:
        return Path(s).expanduser().is_file()
    except Exception:
        return False


# ─── Persistent chat_id cache ─────────────────────────────────────────────────
# Every getUpdates hit is written to disk so a chat_id survives even when another
# process is racing us on long-poll and consuming updates. Small, append-safe.

def _chats_cache_path() -> Path:
    return _state_dir() / "telegram_seen_chats.json"


def _remember_chats_from_updates(updates: list) -> int:
    """Walk raw update payloads, upsert each unique chat into the on-disk cache.
    Returns count of chats newly added or updated."""
    if not updates:
        return 0
    path = _chats_cache_path()
    try:
        existing = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except Exception:
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    changed = 0
    for u in updates:
        m = (
            u.get("message")
            or u.get("edited_message")
            or u.get("channel_post")
            or u.get("callback_query", {}).get("message")
            or {}
        )
        ch = m.get("chat") or {}
        cid = ch.get("id")
        if cid is None:
            continue
        entry = {
            "chat_id": cid,
            "type": ch.get("type"),
            "username": ch.get("username"),
            "first_name": ch.get("first_name"),
            "last_name": ch.get("last_name"),
            "title": ch.get("title"),
            "last_seen_ts": m.get("date") or u.get("update_id"),
            "last_text": (m.get("text") or m.get("caption") or "")[:120],
        }
        key = str(cid)
        if existing.get(key) != entry:
            existing[key] = entry
            changed += 1
    if changed:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass
    return changed


mcp = FastMCP("telegram-bot")


# ─── Account management (multi-bot support) ──────────────────────────────────

@mcp.tool()
def connect_bot(bot_token: str, account: str = "default",
                set_active: bool = True) -> dict:
    """Kết nối 1 bot mới — verify token qua getMe rồi lưu vào secrets.json dưới
    slot `telegram:<account>`. `account` = tên bạn tự đặt để phân biệt (ví dụ
    "prod", "test", "notifier"); trùng tên sẽ ghi đè. `set_active=True` (mặc
    định) → biến bot này thành default cho mọi tool call sau. Trả về getMe
    kèm slot name khi thành công."""
    if not bot_token or ":" not in bot_token:
        return {"error": "invalid bot_token (must be '<id>:<secret>' from @BotFather)"}
    if not account or not account.replace("_", "").replace("-", "").isalnum():
        return {"error": "account name must be alphanumeric (+ _ / -)"}

    # Verify via getMe with the given token
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{bot_token}/getMe",
            timeout=15,
        )
        payload = r.json()
    except Exception as exc:
        return {"error": f"network verify failed: {exc}"}
    if not payload.get("ok"):
        return {"error": f"token rejected by Telegram: {payload.get('description')}"}
    me = payload["result"]

    data = _read_secrets()
    data[f"telegram:{account}"] = {
        "bot_token": bot_token,
        "account": f"@{me.get('username')}" if me.get("username") else None,
        "bot_id": me.get("id"),
        "bot_name": me.get("first_name"),
    }
    if set_active:
        data["telegram:_active"] = {"name": account}
    try:
        _write_secrets(data)
    except Exception as exc:
        return {"error": f"write secrets failed: {exc}"}
    return {
        "ok": True,
        "account": account,
        "active": set_active,
        "bot": {"id": me.get("id"), "username": me.get("username"), "name": me.get("first_name")},
    }


@mcp.tool()
def list_connected_bots() -> dict:
    """Liệt kê MỌI bot đã lưu trong secrets.json (không lộ token). Đánh dấu bot
    đang active."""
    data = _read_secrets()
    active = _active_bot_name(data)
    out = []
    for k, v in data.items():
        if not k.startswith("telegram:") or k == "telegram:_active":
            continue
        name = k.split(":", 1)[1]
        if not isinstance(v, dict) or not v.get("bot_token"):
            continue
        tok = v["bot_token"]
        out.append({
            "account": name,
            "is_active": name == active,
            "username": v.get("account") or v.get("username"),
            "bot_id": v.get("bot_id"),
            "bot_name": v.get("bot_name"),
            "token_id": tok.split(":", 1)[0],  # chỉ show phần ID, giấu secret
        })
    return {"active": active, "accounts": out}


@mcp.tool()
def switch_bot(account: str) -> dict:
    """Đổi bot đang active. Mọi tool call sau (send_message, ...) sẽ dùng bot
    thuộc slot này."""
    data = _read_secrets()
    if f"telegram:{account}" not in data:
        available = [k.split(":", 1)[1] for k in data
                     if k.startswith("telegram:") and k != "telegram:_active"]
        return {"error": f"account '{account}' not connected. Available: {available}"}
    data["telegram:_active"] = {"name": account}
    try:
        _write_secrets(data)
    except Exception as exc:
        return {"error": f"write failed: {exc}"}
    # Verify token still live
    try:
        me = _api("getMe")
    except Exception:
        me = {}
    return {"ok": True, "active": account, "bot": me if not isinstance(me, dict) or "error" not in me else None}


@mcp.tool()
def disconnect_bot(account: str) -> dict:
    """Gỡ 1 bot khỏi secrets. Nếu bot đó đang active → active pointer bị xoá
    (fallback về 'default' nếu vẫn còn)."""
    data = _read_secrets()
    key = f"telegram:{account}"
    if key not in data:
        return {"error": f"account '{account}' not found"}
    del data[key]
    active = _active_bot_name(data)
    if active == account:
        # Fallback: default if it still exists, else first remaining, else clear
        remaining = [k.split(":", 1)[1] for k in data
                     if k.startswith("telegram:") and k != "telegram:_active"
                     and data[k].get("bot_token")]
        if "default" in remaining:
            data["telegram:_active"] = {"name": "default"}
        elif remaining:
            data["telegram:_active"] = {"name": remaining[0]}
        else:
            data.pop("telegram:_active", None)
    try:
        _write_secrets(data)
    except Exception as exc:
        return {"error": f"write failed: {exc}"}
    return {"ok": True, "removed": account, "now_active": _active_bot_name(data) if any(
        k.startswith("telegram:") and k != "telegram:_active" for k in data
    ) else None}


@mcp.tool()
def get_active_bot() -> dict:
    """Trả về slot name + getMe của bot đang active."""
    try:
        name = _active_bot_name()
        me = _api("getMe")
    except Exception as exc:
        return {"error": str(exc)}
    return {"active": name, "bot": me}


@mcp.tool()
def get_me() -> dict:
    """Bot identity — id, username, first_name, permissions. Confirms the token is
    live and the bot is reachable."""
    return _api("getMe")


@mcp.tool()
def get_chat(chat_id: str) -> dict:
    """Fetch metadata for a chat by numeric id (`"123456"`), `@username`, or `-100…`
    for supergroups. Returns type / title / member counts / permissions where
    applicable."""
    return _api("getChat", chat_id=chat_id)


@mcp.tool()
def get_chat_member(chat_id: str, user_id: int) -> dict:
    """Look up a single member of a chat — role (creator/admin/member/kicked), custom
    title, join date. Useful before sending sensitive commands to check the bot's
    own admin status."""
    return _api("getChatMember", chat_id=chat_id, user_id=user_id)


@mcp.tool()
def get_chat_administrators(chat_id: str) -> list[dict]:
    """List every admin in a group/channel with their role. Empty for private chats."""
    r = _api("getChatAdministrators", chat_id=chat_id)
    return r if isinstance(r, list) else [r]


@mcp.tool()
def get_chat_member_count(chat_id: str) -> dict:
    """Total member count of a group/channel."""
    return {"count": _api("getChatMemberCount", chat_id=chat_id)}


@mcp.tool()
def get_updates(limit: int = 20, offset: int = 0, timeout: int = 0) -> list[dict]:
    """Recent updates the bot received (messages sent TO the bot, callbacks, etc.).
    Every hit is upserted into the persistent chat cache — so `list_known_chats`
    still returns the chat_id even if a concurrent long-poller consumed the raw
    update payload. Pass `offset > 0` to ack past updates; `timeout=0` = non-blocking."""
    r = _api("getUpdates", limit=limit, offset=offset or None, timeout=timeout)
    updates = r if isinstance(r, list) else []
    _remember_chats_from_updates(updates)
    return updates


@mcp.tool()
def get_webhook_info() -> dict:
    """Current webhook config for the bot — url, pending_update_count, last_error.
    Empty `url` means the bot is in long-poll mode."""
    return _api("getWebhookInfo")


@mcp.tool()
def send_message(chat_id: str, text: str, parse_mode: str = "",
                 reply_to: int = 0, reply_markup: dict = None,
                 disable_web_page_preview: bool = False,
                 disable_notification: bool = False) -> dict:
    """Send text as the bot. `parse_mode` = "" | "Markdown" | "MarkdownV2" | "HTML".
    `reply_markup` = inline_keyboard / reply_keyboard / remove_keyboard / force_reply
    (use build_inline_keyboard / build_reply_keyboard helpers). Bot can only send
    to chat_ids that have DMed the bot first (Bot API rule)."""
    return _api(
        "sendMessage",
        chat_id=chat_id,
        text=text,
        parse_mode=parse_mode or None,
        reply_to_message_id=reply_to or None,
        reply_markup=reply_markup or None,
        disable_web_page_preview=disable_web_page_preview or None,
        disable_notification=disable_notification or None,
    )


@mcp.tool()
def send_photo(chat_id: str, photo: str, caption: str = "") -> dict:
    """Send a photo. `photo` accepts EITHER a public URL (Telegram fetches it) OR
    a local filesystem path — local paths are uploaded via multipart automatically."""
    if _is_local_file(photo):
        return _api_upload(
            "sendPhoto",
            files={"photo": photo},
            chat_id=chat_id,
            caption=caption or None,
        )
    return _api("sendPhoto", chat_id=chat_id, photo=photo, caption=caption or None)


@mcp.tool()
def send_document(chat_id: str, document: str, caption: str = "") -> dict:
    """Send a document (PDF, ZIP, any file). `document` accepts EITHER a public URL
    (Telegram fetches it) OR a local filesystem path — local paths are uploaded via
    multipart automatically. Bot API cap: 50 MB per file for local uploads."""
    if _is_local_file(document):
        return _api_upload(
            "sendDocument",
            files={"document": document},
            chat_id=chat_id,
            caption=caption or None,
        )
    return _api(
        "sendDocument", chat_id=chat_id, document=document, caption=caption or None
    )


def _extract_filename(url: str, headers: dict, html: str = "") -> str:
    """Best-effort real filename resolution: Content-Disposition → URL basename →
    Google Drive HTML `<title>` (which is `filename - Google Drive`) → empty."""
    import re
    from urllib.parse import unquote, urlparse

    # 1. Content-Disposition: attachment; filename*=UTF-8''foo.rar  OR filename="foo.rar"
    cd = headers.get("content-disposition") or headers.get("Content-Disposition") or ""
    if cd:
        m = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", cd, re.IGNORECASE)
        if m:
            return unquote(m.group(1)).strip('"').strip()
        m = re.search(r'filename\s*=\s*"([^"]+)"', cd, re.IGNORECASE)
        if m:
            return m.group(1).strip()
        m = re.search(r"filename\s*=\s*([^;]+)", cd, re.IGNORECASE)
        if m:
            return m.group(1).strip().strip('"')

    # 2. Google Drive HTML title: "<filename> - Google Drive"
    if html and "drive.google.com" in url:
        m = re.search(r"<title>([^<]+?)\s*-\s*Google Drive</title>", html, re.IGNORECASE)
        if m:
            return m.group(1).strip()

    # 3. URL path basename (skip if it's just "uc" or empty)
    path = urlparse(url).path
    base = Path(unquote(path)).name
    if base and base not in ("uc", "download", "view", ""):
        return base

    return ""


@mcp.tool()
def download_and_send_document(
    chat_id: str, source_url: str, caption: str = "", filename: str = ""
) -> dict:
    """Fetch `source_url` → upload via multipart to `chat_id`. Handles Google Drive
    share URLs (rewrites → uc?id=…, follows confirm-token/form dance for files
    >25MB), preserves the real filename from Content-Disposition, and cleans up
    the temp file even on error. Pass `filename` to override the name shown in
    Telegram."""
    import re
    import tempfile
    from urllib.parse import parse_qs, urlparse

    original_url = source_url

    # Rewrite Drive share URL → direct download endpoint
    m = re.search(r"drive\.google\.com/file/d/([^/]+)/", source_url)
    if m:
        source_url = f"https://drive.google.com/uc?export=download&id={m.group(1)}"

    tmp_path = None
    try:
        with httpx.Client(follow_redirects=True, timeout=300) as c:
            r = c.get(source_url)

            # Google Drive interstitial for files >25MB: the response is an HTML
            # form ("Google Drive can't scan this file for viruses…"). Extract
            # every hidden input and POST to the form's action.
            ctype = (r.headers.get("content-type") or "").lower()
            html_snapshot = ""
            if "text/html" in ctype and "drive.google.com" in source_url:
                html_snapshot = r.text
                form_action = re.search(r'<form[^>]+action="([^"]+)"', html_snapshot)
                if form_action:
                    action_url = form_action.group(1).replace("&amp;", "&")
                    hidden = dict(
                        re.findall(
                            r'<input[^>]+name="([^"]+)"[^>]+value="([^"]*)"',
                            html_snapshot,
                        )
                    )
                    r = c.get(action_url, params=hidden)
                else:
                    tok = re.search(r"confirm=([0-9A-Za-z_-]+)", html_snapshot)
                    if tok:
                        r = c.get(source_url + f"&confirm={tok.group(1)}")

            r.raise_for_status()

            # Resolve real filename BEFORE writing so the temp file gets the right suffix
            resolved = filename or _extract_filename(
                str(r.url), dict(r.headers), html_snapshot
            )
            if not resolved:
                # Last resort: derive from query param `id` when it's a Drive uc URL
                qs = parse_qs(urlparse(source_url).query)
                if qs.get("id"):
                    resolved = f"drive_{qs['id'][0][:8]}.bin"
                else:
                    resolved = "download.bin"

            # Write to a temp dir under the desired name — Telegram uses this name
            tmp_dir = Path(tempfile.mkdtemp(prefix="tg_dl_"))
            tmp_path = str(tmp_dir / resolved)
            Path(tmp_path).write_bytes(r.content)
    except Exception as exc:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
        return {"error": f"download failed: {exc}", "source_url": original_url}

    try:
        result = _api_upload(
            "sendDocument",
            files={"document": tmp_path},
            chat_id=chat_id,
            caption=caption or None,
        )
        # Surface the resolved filename + local size so caller can verify
        if isinstance(result, dict) and "error" not in result:
            result["_downloaded_filename"] = Path(tmp_path).name
            result["_downloaded_size"] = Path(tmp_path).stat().st_size
        return result
    finally:
        try:
            os.unlink(tmp_path)
            os.rmdir(Path(tmp_path).parent)
        except Exception:
            pass


@mcp.tool()
def edit_message_text(chat_id: str, message_id: int, text: str, parse_mode: str = "") -> dict:
    """Edit a text message the bot previously sent. Only works within ~48h of send."""
    return _api(
        "editMessageText",
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        parse_mode=parse_mode or None,
    )


@mcp.tool()
def delete_message(chat_id: str, message_id: int) -> dict:
    """Delete a message the bot sent (or any message in a group where bot is admin).
    Bots can only delete their own messages in private chats."""
    return {"ok": bool(_api("deleteMessage", chat_id=chat_id, message_id=message_id))}


@mcp.tool()
def forward_message(from_chat_id: str, to_chat_id: str, message_id: int) -> dict:
    """Copy-forward a message from one chat to another. Bot must see both chats."""
    return _api(
        "forwardMessage",
        chat_id=to_chat_id,
        from_chat_id=from_chat_id,
        message_id=message_id,
    )


@mcp.tool()
def set_my_commands(commands: list[dict]) -> dict:
    """Update the / command menu shown to users in the bot's chat.
    `commands` = [{"command": "start", "description": "..."}, ...]. Empty list clears."""
    return {"ok": bool(_api("setMyCommands", commands=commands))}


# ─── Bulk / compound operations ────────────────────────────────────────────────

@mcp.tool()
def list_recent_senders(limit: int = 100) -> list[dict]:
    """Trả về list user ĐÃ TỪNG nhắn cho bot (lấy từ getUpdates gần nhất, unique
    theo user_id). Mỗi entry: {user_id, username, first_name, last_message,
    last_date}. Cũng cache chat_id vào disk — nếu bị script khác consume updates,
    dùng `list_known_chats` để đọc từ cache thay vì gọi Bot API."""
    updates = _api("getUpdates", limit=limit, timeout=0)
    if not isinstance(updates, list):
        return []
    _remember_chats_from_updates(updates)
    seen: dict[int, dict] = {}
    for u in updates:
        m = u.get("message") or u.get("edited_message") or {}
        frm = m.get("from") or {}
        uid = frm.get("id")
        if not uid:
            continue
        seen[uid] = {
            "user_id": uid,
            "username": frm.get("username"),
            "first_name": frm.get("first_name"),
            "last_message": (m.get("text") or "")[:100],
            "last_date": m.get("date"),
        }
    return list(seen.values())


@mcp.tool()
def list_known_chats() -> list[dict]:
    """Đọc chat_id cache tích luỹ trên disk (`telegram_seen_chats.json` trong
    coworker state dir). MỖI lần bridge nhìn thấy 1 update — dù qua get_updates,
    list_recent_senders, hay bất cứ tool nào — chat_id được ghi vào đây. Điều
    này giúp lấy được chat_id ngay cả khi script khác đang giữ khóa long-poll và
    làm `get_updates` trả rỗng."""
    path = _chats_cache_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    return list(data.values())


@mcp.tool()
def remember_chat(
    chat_id: int, username: str = "", first_name: str = "", note: str = ""
) -> dict:
    """Thủ công lưu 1 chat_id vào cache (khi user paste chat_id trực tiếp thay vì
    để bridge scrape từ getUpdates). Tránh phải nhắn lại bot lần sau."""
    path = _chats_cache_path()
    try:
        existing = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except Exception:
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    entry = {
        "chat_id": chat_id,
        "type": "private",
        "username": username or None,
        "first_name": first_name or None,
        "note": note or None,
        "source": "manual",
    }
    existing[str(chat_id)] = entry
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as exc:
        return {"error": f"write failed: {exc}"}
    return {"ok": True, "chat_id": chat_id, "total_cached": len(existing)}


@mcp.tool()
def broadcast_to_recent_senders(text: str, limit: int = 100, parse_mode: str = "") -> dict:
    """Gửi cùng 1 tin `text` cho MỌI user đã từng DM bot gần đây. Auto-fetch danh
    sách qua `getUpdates`; nếu rỗng (script khác đang consume) fallback về
    `list_known_chats` cache. Trả tổng số sent + failed + error details."""
    users = list_recent_senders(limit=limit)
    if not users:
        # Fallback: persisted cache still knows every chat we've ever seen.
        users = [
            {
                "user_id": c["chat_id"],
                "username": c.get("username"),
                "first_name": c.get("first_name"),
            }
            for c in list_known_chats()
            if c.get("type") in (None, "private")
        ]
    if not users:
        return {"error": "no recent senders and cache is empty — call remember_chat manually"}
    sent, failed = [], []
    for u in users:
        r = _api(
            "sendMessage",
            chat_id=u["user_id"],
            text=text,
            parse_mode=parse_mode or None,
        )
        if "error" in r:
            failed.append({"user_id": u["user_id"], "error": r["error"]})
        else:
            sent.append({"user_id": u["user_id"], "message_id": r.get("message_id")})
    return {
        "total_targets": len(users),
        "sent": len(sent),
        "failed": len(failed),
        "sent_details": sent,
        "failed_details": failed,
    }


@mcp.tool()
def broadcast_to_chat_ids(chat_ids: list, text: str, parse_mode: str = "") -> dict:
    """Gửi 1 tin `text` cho danh sách chat_ids do bạn cung cấp. Trả sent/failed
    detail. Dùng khi bạn đã có danh sách user_id sẵn (từ DB, file, ...)."""
    sent, failed = [], []
    for cid in chat_ids:
        r = _api("sendMessage", chat_id=cid, text=text, parse_mode=parse_mode or None)
        if "error" in r:
            failed.append({"chat_id": cid, "error": r["error"]})
        else:
            sent.append({"chat_id": cid, "message_id": r.get("message_id")})
    return {
        "total": len(chat_ids),
        "sent": len(sent),
        "failed": len(failed),
        "failed_details": failed[:20],
    }


@mcp.tool()
def stats_recent_senders(limit: int = 100) -> dict:
    """Thống kê users DM bot gần đây: tổng unique, top messagers, phân bố theo giờ."""
    updates = _api("getUpdates", limit=limit, timeout=0)
    if not isinstance(updates, list):
        return {"error": "no updates available"}
    per_user: dict[int, int] = {}
    per_hour: dict[int, int] = {}
    for u in updates:
        m = u.get("message") or u.get("edited_message") or {}
        frm = m.get("from") or {}
        uid = frm.get("id")
        if uid:
            per_user[uid] = per_user.get(uid, 0) + 1
        date = m.get("date")
        if date:
            from datetime import datetime, timezone
            hour = datetime.fromtimestamp(date, tz=timezone.utc).hour
            per_hour[hour] = per_hour.get(hour, 0) + 1
    top = sorted(per_user.items(), key=lambda x: -x[1])[:10]
    return {
        "total_updates": len(updates),
        "unique_users": len(per_user),
        "top_10_users": [{"user_id": u, "count": c} for u, c in top],
        "hour_distribution": dict(sorted(per_hour.items())),
    }


# ─── Media send (all accept local path OR URL) ────────────────────────────────

def _send_media(method: str, field: str, chat_id: str, source: str, **extra: Any) -> dict:
    """Common dispatcher for sendPhoto/sendVideo/sendAudio/…: if `source` is a
    local file → multipart upload, else pass the URL/file_id to Telegram."""
    if _is_local_file(source):
        return _api_upload(method, files={field: source}, chat_id=chat_id, **extra)
    return _api(method, chat_id=chat_id, **{field: source}, **extra)


@mcp.tool()
def send_video(chat_id: str, video: str, caption: str = "", duration: int = 0,
               width: int = 0, height: int = 0, supports_streaming: bool = True) -> dict:
    """Gửi video. `video` = URL, file_id, hoặc local path. Local path tự upload
    multipart. `supports_streaming=True` bật streaming trong Telegram client."""
    return _send_media(
        "sendVideo", "video", chat_id, video,
        caption=caption or None,
        duration=duration or None,
        width=width or None,
        height=height or None,
        supports_streaming=supports_streaming,
    )


@mcp.tool()
def send_audio(chat_id: str, audio: str, caption: str = "", duration: int = 0,
               performer: str = "", title: str = "") -> dict:
    """Gửi audio (mp3/m4a). Telegram render như music player. Local path OK."""
    return _send_media(
        "sendAudio", "audio", chat_id, audio,
        caption=caption or None,
        duration=duration or None,
        performer=performer or None,
        title=title or None,
    )


@mcp.tool()
def send_voice(chat_id: str, voice: str, caption: str = "", duration: int = 0) -> dict:
    """Gửi voice note (ogg/opus). Hiện như voice message bubble."""
    return _send_media(
        "sendVoice", "voice", chat_id, voice,
        caption=caption or None,
        duration=duration or None,
    )


@mcp.tool()
def send_video_note(chat_id: str, video_note: str, duration: int = 0, length: int = 0) -> dict:
    """Gửi circular video note (mp4 vuông, ≤60s). Hiện như video tròn nổi bật."""
    return _send_media(
        "sendVideoNote", "video_note", chat_id, video_note,
        duration=duration or None,
        length=length or None,
    )


@mcp.tool()
def send_animation(chat_id: str, animation: str, caption: str = "", duration: int = 0,
                   width: int = 0, height: int = 0) -> dict:
    """Gửi GIF/mp4 animation. Telegram tự loop."""
    return _send_media(
        "sendAnimation", "animation", chat_id, animation,
        caption=caption or None,
        duration=duration or None,
        width=width or None,
        height=height or None,
    )


@mcp.tool()
def send_sticker(chat_id: str, sticker: str, emoji: str = "") -> dict:
    """Gửi sticker. `sticker` = file_id (từ sticker pack) hoặc URL/local WebP."""
    return _send_media(
        "sendSticker", "sticker", chat_id, sticker,
        emoji=emoji or None,
    )


@mcp.tool()
def send_media_group(chat_id: str, media: list, disable_notification: bool = False) -> dict:
    """Gửi album (2-10 photo/video cùng lúc). `media` = list of
    [{"type":"photo","media":"path_or_url","caption":"..."}, ...]. Local paths
    được auto-upload multipart với ref `attach://file_N`."""
    if not media or len(media) < 2 or len(media) > 10:
        return {"error": "media must have 2..10 items"}
    files = {}
    payload = []
    for i, item in enumerate(media):
        m = dict(item)
        src = m.get("media", "")
        if _is_local_file(src):
            key = f"file_{i}"
            files[key] = src
            m["media"] = f"attach://{key}"
        payload.append(m)
    if files:
        return _api_upload(
            "sendMediaGroup",
            files=files,
            chat_id=chat_id,
            media=json.dumps(payload, ensure_ascii=False),
            disable_notification=disable_notification or None,
        )
    return _api(
        "sendMediaGroup",
        chat_id=chat_id,
        media=payload,
        disable_notification=disable_notification or None,
    )


@mcp.tool()
def send_location(chat_id: str, latitude: float, longitude: float,
                  live_period: int = 0, horizontal_accuracy: float = 0) -> dict:
    """Gửi vị trí tĩnh. `live_period` (60..86400 giây) biến thành live location."""
    return _api(
        "sendLocation",
        chat_id=chat_id,
        latitude=latitude,
        longitude=longitude,
        live_period=live_period or None,
        horizontal_accuracy=horizontal_accuracy or None,
    )


@mcp.tool()
def send_venue(chat_id: str, latitude: float, longitude: float, title: str,
               address: str, foursquare_id: str = "") -> dict:
    """Gửi địa điểm với tên + địa chỉ (card map). foursquare_id optional."""
    return _api(
        "sendVenue",
        chat_id=chat_id,
        latitude=latitude, longitude=longitude,
        title=title, address=address,
        foursquare_id=foursquare_id or None,
    )


@mcp.tool()
def send_contact(chat_id: str, phone_number: str, first_name: str,
                 last_name: str = "", vcard: str = "") -> dict:
    """Gửi contact card."""
    return _api(
        "sendContact",
        chat_id=chat_id,
        phone_number=phone_number, first_name=first_name,
        last_name=last_name or None, vcard=vcard or None,
    )


@mcp.tool()
def send_poll(chat_id: str, question: str, options: list, is_anonymous: bool = True,
              allows_multiple_answers: bool = False, poll_type: str = "regular",
              correct_option_id: int = -1, explanation: str = "") -> dict:
    """Gửi poll. `poll_type` = "regular" | "quiz". Quiz: đặt `correct_option_id`."""
    body = {
        "chat_id": chat_id,
        "question": question,
        "options": options,
        "is_anonymous": is_anonymous,
        "allows_multiple_answers": allows_multiple_answers,
        "type": poll_type,
    }
    if poll_type == "quiz" and correct_option_id >= 0:
        body["correct_option_id"] = correct_option_id
        if explanation:
            body["explanation"] = explanation
    return _api("sendPoll", **body)


@mcp.tool()
def send_dice(chat_id: str, emoji: str = "🎲") -> dict:
    """Gửi animated dice. `emoji` ∈ {🎲, 🎯, 🏀, ⚽, 🎳, 🎰}. Trả kết quả 1..6."""
    return _api("sendDice", chat_id=chat_id, emoji=emoji)


@mcp.tool()
def send_chat_action(chat_id: str, action: str = "typing") -> dict:
    """Hiện indicator "typing…" / "sending photo…". `action` ∈ typing |
    upload_photo | record_video | upload_video | record_voice | upload_voice |
    upload_document | choose_sticker | find_location | record_video_note |
    upload_video_note. Kéo dài ~5s hoặc đến message tiếp theo."""
    return {"ok": bool(_api("sendChatAction", chat_id=chat_id, action=action))}


# ─── Message manipulation ─────────────────────────────────────────────────────

@mcp.tool()
def copy_message(from_chat_id: str, to_chat_id: str, message_id: int,
                 caption: str = "") -> dict:
    """Copy 1 message sang chat khác — KHÔNG hiện "Forwarded from". `caption` mới
    ghi đè caption gốc nếu có."""
    return _api(
        "copyMessage",
        chat_id=to_chat_id, from_chat_id=from_chat_id, message_id=message_id,
        caption=caption or None,
    )


@mcp.tool()
def edit_message_caption(chat_id: str, message_id: int, caption: str,
                         parse_mode: str = "") -> dict:
    """Sửa caption của media message đã gửi."""
    return _api(
        "editMessageCaption",
        chat_id=chat_id, message_id=message_id,
        caption=caption, parse_mode=parse_mode or None,
    )


@mcp.tool()
def edit_message_reply_markup(chat_id: str, message_id: int,
                              reply_markup: dict) -> dict:
    """Sửa inline keyboard của message. `reply_markup` = {"inline_keyboard": [[{...}]]}."""
    return _api(
        "editMessageReplyMarkup",
        chat_id=chat_id, message_id=message_id,
        reply_markup=reply_markup,
    )


@mcp.tool()
def set_message_reaction(chat_id: str, message_id: int,
                         reactions: list, is_big: bool = False) -> dict:
    """Đặt reaction cho message. `reactions` = list emoji (["👍","🔥"]) hoặc empty
    để xoá. Bot chỉ được set 1 reaction / message trong hầu hết chat."""
    payload = [{"type": "emoji", "emoji": r} if isinstance(r, str) else r for r in reactions]
    return {
        "ok": bool(_api(
            "setMessageReaction",
            chat_id=chat_id, message_id=message_id,
            reaction=payload, is_big=is_big or None,
        ))
    }


@mcp.tool()
def pin_chat_message(chat_id: str, message_id: int,
                     disable_notification: bool = False) -> dict:
    """Ghim message trong chat (bot cần quyền admin ở group/channel)."""
    return {"ok": bool(_api(
        "pinChatMessage", chat_id=chat_id, message_id=message_id,
        disable_notification=disable_notification or None,
    ))}


@mcp.tool()
def unpin_chat_message(chat_id: str, message_id: int = 0) -> dict:
    """Bỏ ghim 1 message (không truyền id → bỏ ghim message ghim gần nhất)."""
    return {"ok": bool(_api(
        "unpinChatMessage", chat_id=chat_id, message_id=message_id or None,
    ))}


@mcp.tool()
def unpin_all_chat_messages(chat_id: str) -> dict:
    """Bỏ ghim MỌI message trong chat."""
    return {"ok": bool(_api("unpinAllChatMessages", chat_id=chat_id))}


# ─── File download (bot's uploaded files) ─────────────────────────────────────

@mcp.tool()
def get_file(file_id: str) -> dict:
    """Lấy metadata + relative path của file bot đã upload (hoặc file gửi TO bot).
    Trả {file_id, file_unique_id, file_size, file_path}. Sau đó download qua
    `download_file`."""
    return _api("getFile", file_id=file_id)


@mcp.tool()
def download_file(file_id: str, save_to: str = "") -> dict:
    """Tải file từ Telegram servers về local. `save_to` = absolute path (nếu rỗng
    → dùng temp dir). Trả {path, size, file_name}."""
    import tempfile
    meta = _api("getFile", file_id=file_id)
    if "error" in meta:
        return meta
    fp = meta.get("file_path")
    if not fp:
        return {"error": "no file_path in getFile response"}
    try:
        token = _load_bot_token()
    except Exception as exc:
        return {"error": str(exc)}
    url = f"https://api.telegram.org/file/bot{token}/{fp}"
    if save_to:
        target = Path(save_to).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
    else:
        target = Path(tempfile.gettempdir()) / Path(fp).name
    try:
        with httpx.Client(timeout=300) as c:
            r = c.get(url); r.raise_for_status()
            target.write_bytes(r.content)
    except Exception as exc:
        return {"error": f"download failed: {exc}"}
    return {"path": str(target), "size": target.stat().st_size, "file_name": target.name}


# ─── Chat administration ──────────────────────────────────────────────────────

@mcp.tool()
def ban_chat_member(chat_id: str, user_id: int, until_date: int = 0,
                    revoke_messages: bool = False) -> dict:
    """Ban user khỏi group/channel. `until_date` = unix ts (0 = ban vĩnh viễn).
    `revoke_messages=True` xoá luôn mọi message của user đó."""
    return {"ok": bool(_api(
        "banChatMember", chat_id=chat_id, user_id=user_id,
        until_date=until_date or None,
        revoke_messages=revoke_messages or None,
    ))}


@mcp.tool()
def unban_chat_member(chat_id: str, user_id: int, only_if_banned: bool = True) -> dict:
    """Unban user. `only_if_banned=True` → chỉ unban nếu đang ban (không auto-add)."""
    return {"ok": bool(_api(
        "unbanChatMember", chat_id=chat_id, user_id=user_id,
        only_if_banned=only_if_banned,
    ))}


@mcp.tool()
def restrict_chat_member(chat_id: str, user_id: int, permissions: dict,
                         until_date: int = 0) -> dict:
    """Mute/hạn chế user trong group. `permissions` = {can_send_messages, can_send_photos,
    can_send_videos, can_send_audios, can_send_documents, can_send_polls, ...}"""
    return {"ok": bool(_api(
        "restrictChatMember", chat_id=chat_id, user_id=user_id,
        permissions=permissions, until_date=until_date or None,
    ))}


@mcp.tool()
def promote_chat_member(chat_id: str, user_id: int, rights: dict) -> dict:
    """Thăng admin / gỡ admin. `rights` = {can_manage_chat, can_delete_messages,
    can_manage_video_chats, can_restrict_members, can_promote_members,
    can_change_info, can_invite_users, can_pin_messages, ...} — tất cả False = demote."""
    return {"ok": bool(_api("promoteChatMember", chat_id=chat_id, user_id=user_id, **rights))}


@mcp.tool()
def set_chat_title(chat_id: str, title: str) -> dict:
    """Đổi tên group/channel (bot cần quyền can_change_info)."""
    return {"ok": bool(_api("setChatTitle", chat_id=chat_id, title=title))}


@mcp.tool()
def set_chat_description(chat_id: str, description: str) -> dict:
    """Đổi description group/channel."""
    return {"ok": bool(_api("setChatDescription", chat_id=chat_id, description=description))}


@mcp.tool()
def set_chat_photo(chat_id: str, photo: str) -> dict:
    """Đặt ảnh chat. `photo` = local path (upload multipart)."""
    if not _is_local_file(photo):
        return {"error": "set_chat_photo requires a local file path"}
    return _api_upload("setChatPhoto", files={"photo": photo}, chat_id=chat_id)


@mcp.tool()
def delete_chat_photo(chat_id: str) -> dict:
    """Xoá ảnh chat."""
    return {"ok": bool(_api("deleteChatPhoto", chat_id=chat_id))}


@mcp.tool()
def leave_chat(chat_id: str) -> dict:
    """Bot rời group/channel."""
    return {"ok": bool(_api("leaveChat", chat_id=chat_id))}


@mcp.tool()
def export_chat_invite_link(chat_id: str) -> dict:
    """Lấy invite link chính của chat (revoke link cũ). Trả string URL."""
    r = _api("exportChatInviteLink", chat_id=chat_id)
    return {"invite_link": r} if isinstance(r, str) else r


@mcp.tool()
def create_chat_invite_link(chat_id: str, name: str = "", expire_date: int = 0,
                            member_limit: int = 0, creates_join_request: bool = False) -> dict:
    """Tạo invite link mới (có expire/limit/require-approval). Không revoke link cũ."""
    return _api(
        "createChatInviteLink",
        chat_id=chat_id, name=name or None,
        expire_date=expire_date or None, member_limit=member_limit or None,
        creates_join_request=creates_join_request or None,
    )


@mcp.tool()
def revoke_chat_invite_link(chat_id: str, invite_link: str) -> dict:
    """Vô hiệu hoá 1 invite link cụ thể."""
    return _api("revokeChatInviteLink", chat_id=chat_id, invite_link=invite_link)


@mcp.tool()
def approve_chat_join_request(chat_id: str, user_id: int) -> dict:
    """Duyệt yêu cầu join (từ link `creates_join_request=True`)."""
    return {"ok": bool(_api("approveChatJoinRequest", chat_id=chat_id, user_id=user_id))}


@mcp.tool()
def decline_chat_join_request(chat_id: str, user_id: int) -> dict:
    """Từ chối yêu cầu join."""
    return {"ok": bool(_api("declineChatJoinRequest", chat_id=chat_id, user_id=user_id))}


# ─── Bot self-metadata ────────────────────────────────────────────────────────

@mcp.tool()
def set_my_name(name: str, language_code: str = "") -> dict:
    """Đổi display name của bot (tối đa 64 ký tự). `language_code` = "" cho default."""
    return {"ok": bool(_api("setMyName", name=name, language_code=language_code or None))}


@mcp.tool()
def get_my_name(language_code: str = "") -> dict:
    """Đọc display name hiện tại."""
    return _api("getMyName", language_code=language_code or None)


@mcp.tool()
def set_my_description(description: str, language_code: str = "") -> dict:
    """Đổi mô tả bot (hiện ở screen trống trước khi user /start). Tối đa 512 ký tự."""
    return {"ok": bool(_api(
        "setMyDescription", description=description,
        language_code=language_code or None,
    ))}


@mcp.tool()
def get_my_description(language_code: str = "") -> dict:
    """Đọc description hiện tại."""
    return _api("getMyDescription", language_code=language_code or None)


@mcp.tool()
def set_my_short_description(short_description: str, language_code: str = "") -> dict:
    """Đổi short description (hiện ở profile bot, tối đa 120 ký tự)."""
    return {"ok": bool(_api(
        "setMyShortDescription", short_description=short_description,
        language_code=language_code or None,
    ))}


@mcp.tool()
def get_my_commands(language_code: str = "") -> list[dict]:
    """Đọc danh sách / command hiện tại."""
    r = _api("getMyCommands", language_code=language_code or None)
    return r if isinstance(r, list) else []


@mcp.tool()
def delete_my_commands(language_code: str = "") -> dict:
    """Xoá hết / command menu."""
    return {"ok": bool(_api("deleteMyCommands", language_code=language_code or None))}


@mcp.tool()
def set_chat_menu_button(chat_id: int = 0, menu_button: dict = None) -> dict:
    """Đổi nút menu bên trái ô nhập chat. `menu_button` = {"type":"commands"} |
    {"type":"web_app","text":"...","web_app":{"url":"..."}} | {"type":"default"}.
    `chat_id=0` → áp dụng cho mọi chat mặc định."""
    return {"ok": bool(_api(
        "setChatMenuButton",
        chat_id=chat_id or None,
        menu_button=menu_button or {"type": "default"},
    ))}


@mcp.tool()
def get_chat_menu_button(chat_id: int = 0) -> dict:
    """Đọc menu button hiện tại."""
    return _api("getChatMenuButton", chat_id=chat_id or None)


# ─── Webhook & callback answers ───────────────────────────────────────────────

@mcp.tool()
def set_webhook(url: str, secret_token: str = "", allowed_updates: list = None,
                drop_pending_updates: bool = False) -> dict:
    """Đăng ký webhook (thay long-poll). URL phải HTTPS. `secret_token` được gửi
    trong header `X-Telegram-Bot-Api-Secret-Token` để verify request."""
    return {"ok": bool(_api(
        "setWebhook", url=url,
        secret_token=secret_token or None,
        allowed_updates=allowed_updates or None,
        drop_pending_updates=drop_pending_updates or None,
    ))}


@mcp.tool()
def delete_webhook(drop_pending_updates: bool = False) -> dict:
    """Gỡ webhook — bot quay về long-poll mode."""
    return {"ok": bool(_api(
        "deleteWebhook", drop_pending_updates=drop_pending_updates or None,
    ))}


@mcp.tool()
def answer_callback_query(callback_query_id: str, text: str = "",
                          show_alert: bool = False, url: str = "",
                          cache_time: int = 0) -> dict:
    """Trả lời inline button click. `show_alert=True` hiện popup thay vì toast."""
    return {"ok": bool(_api(
        "answerCallbackQuery",
        callback_query_id=callback_query_id,
        text=text or None, show_alert=show_alert or None,
        url=url or None, cache_time=cache_time or None,
    ))}


@mcp.tool()
def answer_inline_query(inline_query_id: str, results: list,
                        cache_time: int = 300, is_personal: bool = False) -> dict:
    """Trả kết quả cho @bot inline query. `results` = list InlineQueryResult objects."""
    return {"ok": bool(_api(
        "answerInlineQuery",
        inline_query_id=inline_query_id, results=results,
        cache_time=cache_time or None, is_personal=is_personal or None,
    ))}


# ─── Bulk document broadcast ──────────────────────────────────────────────────

@mcp.tool()
def broadcast_document_to_chat_ids(chat_ids: list, document: str,
                                   caption: str = "") -> dict:
    """Gửi 1 file (local path HOẶC URL) cho nhiều chat_ids. Với local file, upload
    1 lần rồi reuse `file_id` cho các chat sau — tránh re-upload."""
    if not chat_ids:
        return {"error": "empty chat_ids"}
    sent, failed = [], []
    reuse_id = None
    for cid in chat_ids:
        payload = reuse_id or document
        r = send_document(str(cid), payload, caption=caption)
        if "error" in r:
            failed.append({"chat_id": cid, "error": r["error"]})
        else:
            sent.append({"chat_id": cid, "message_id": r.get("message_id")})
            if reuse_id is None:
                # Grab uploaded file_id for cheap reuse on subsequent sends.
                doc = r.get("document") or {}
                reuse_id = doc.get("file_id") or reuse_id
    return {
        "total": len(chat_ids),
        "sent": len(sent),
        "failed": len(failed),
        "reused_file_id": reuse_id,
        "failed_details": failed[:20],
    }


# ─── Keyboard / reply-markup builders ─────────────────────────────────────────

@mcp.tool()
def build_inline_keyboard(rows: list) -> dict:
    """Xây inline keyboard cho `reply_markup`. `rows` = [[{...}, {...}], [...]].
    Mỗi button: {"text":"OK", "callback_data":"ok"} | {"text":"Open","url":"https://…"}
    | {"text":"Login","login_url":{...}} | {"text":"Switch","switch_inline_query":"q"}.
    Trả về dict {"inline_keyboard": rows} — paste thẳng vào send_message(reply_markup=…)."""
    return {"inline_keyboard": rows}


@mcp.tool()
def build_reply_keyboard(rows: list, one_time_keyboard: bool = False,
                         resize_keyboard: bool = True,
                         input_field_placeholder: str = "",
                         selective: bool = False) -> dict:
    """Xây custom reply keyboard (buttons dưới ô nhập chat). `rows` = [["Yes","No"],
    ["Cancel"]] hoặc [[{"text":"Share phone","request_contact":true}], …].
    `one_time_keyboard=True` → tự ẩn sau khi user tap."""
    normalized = []
    for row in rows:
        normalized.append([
            b if isinstance(b, dict) else {"text": str(b)} for b in row
        ])
    body = {
        "keyboard": normalized,
        "one_time_keyboard": one_time_keyboard,
        "resize_keyboard": resize_keyboard,
    }
    if input_field_placeholder:
        body["input_field_placeholder"] = input_field_placeholder
    if selective:
        body["selective"] = True
    return body


@mcp.tool()
def remove_keyboard(selective: bool = False) -> dict:
    """Trả về `reply_markup` gỡ custom keyboard hiện tại."""
    return {"remove_keyboard": True, "selective": selective}


@mcp.tool()
def force_reply(input_field_placeholder: str = "", selective: bool = False) -> dict:
    """Trả về `reply_markup` bật ô reply-focus cho user — client tự mở bàn phím
    và focus vào ô nhập, coi như user đang reply chính message này."""
    body = {"force_reply": True}
    if input_field_placeholder:
        body["input_field_placeholder"] = input_field_placeholder
    if selective:
        body["selective"] = True
    return body


# ─── Text-formatting escape helpers (tránh MarkdownV2 parse error) ───────────

@mcp.tool()
def escape_markdown_v2(text: str) -> str:
    """Escape ký tự đặc biệt cho `parse_mode="MarkdownV2"`. Không dùng cho text
    NẰM TRONG code block (chỉ escape `\\` và `` ` `` trong đó)."""
    import re
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!\\])", r"\\\1", text)


@mcp.tool()
def escape_html(text: str) -> str:
    """Escape cho `parse_mode="HTML"` — chỉ 3 ký tự < > &."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ─── Reactive: listen / wait for user input ───────────────────────────────────

@mcp.tool()
def wait_for_reply(chat_id: int = 0, timeout_seconds: int = 60,
                   text_contains: str = "", since_update_id: int = 0) -> dict:
    """Long-poll đến khi có message mới thoả điều kiện, hoặc timeout.
      * `chat_id` = 0 → nhận từ bất kỳ chat nào.
      * `text_contains` = "" → không lọc theo nội dung.
      * `since_update_id` = 0 → chỉ bắt update mới sau khi bắt đầu poll.
    Trả về {message, update_id, chat_id, text, from_user} hoặc {"timeout": True}.
    WARNING: nếu bot đang bị script khác long-poll cùng token → sẽ nhận HTTP 409.
    Trong trường hợp đó, dùng webhook mode (set_webhook) hoặc dừng script kia."""
    import time
    deadline = time.time() + max(1, timeout_seconds)
    offset = since_update_id + 1 if since_update_id else 0
    conflicts = 0
    while time.time() < deadline:
        remaining = max(1, int(deadline - time.time()))
        r = _api("getUpdates", offset=offset or None, timeout=min(remaining, 25), limit=50)
        if isinstance(r, dict) and "error" in r:
            if "Conflict" in str(r["error"]) or "409" in str(r["error"]):
                conflicts += 1
                if conflicts >= 3:
                    return {"error": "another poller is consuming updates (409 conflict) — stop competing script or use webhook"}
                time.sleep(1)
                continue
            return r
        updates = r if isinstance(r, list) else []
        _remember_chats_from_updates(updates)
        for u in updates:
            offset = u["update_id"] + 1
            m = u.get("message") or u.get("edited_message") or {}
            if not m:
                continue
            cid = (m.get("chat") or {}).get("id")
            if chat_id and cid != chat_id:
                continue
            txt = m.get("text") or m.get("caption") or ""
            if text_contains and text_contains.lower() not in txt.lower():
                continue
            frm = m.get("from") or {}
            return {
                "update_id": u["update_id"],
                "message_id": m.get("message_id"),
                "chat_id": cid,
                "text": txt,
                "from_user": {
                    "id": frm.get("id"),
                    "username": frm.get("username"),
                    "first_name": frm.get("first_name"),
                },
                "has_document": bool(m.get("document")),
                "has_photo": bool(m.get("photo")),
                "has_video": bool(m.get("video")),
                "has_audio": bool(m.get("audio") or m.get("voice")),
                "raw_message": m,
            }
    return {"timeout": True, "polled_until_offset": offset}


@mcp.tool()
def wait_for_callback(callback_data_starts_with: str = "",
                      chat_id: int = 0, timeout_seconds: int = 60,
                      since_update_id: int = 0) -> dict:
    """Chờ user tap inline button. Filter theo prefix `callback_data`. Trả về
    {callback_query_id, data, from_user, message_id, chat_id} — nhớ gọi
    `answer_callback_query(...)` để dismiss loading spinner."""
    import time
    deadline = time.time() + max(1, timeout_seconds)
    offset = since_update_id + 1 if since_update_id else 0
    while time.time() < deadline:
        remaining = max(1, int(deadline - time.time()))
        r = _api("getUpdates", offset=offset or None, timeout=min(remaining, 25),
                 limit=50, allowed_updates=["callback_query"])
        updates = r if isinstance(r, list) else []
        for u in updates:
            offset = u["update_id"] + 1
            cb = u.get("callback_query")
            if not cb:
                continue
            data = cb.get("data") or ""
            if callback_data_starts_with and not data.startswith(callback_data_starts_with):
                continue
            msg = cb.get("message") or {}
            cid = (msg.get("chat") or {}).get("id")
            if chat_id and cid != chat_id:
                continue
            return {
                "callback_query_id": cb.get("id"),
                "data": data,
                "from_user": cb.get("from"),
                "message_id": msg.get("message_id"),
                "chat_id": cid,
                "update_id": u["update_id"],
            }
    return {"timeout": True, "polled_until_offset": offset}


# ─── Command dispatcher (run a mini bot server for N seconds) ────────────────

@mcp.tool()
def run_command_bot(commands: dict, duration_seconds: int = 60,
                    fallback_reply: str = "") -> dict:
    """Chạy 1 vòng long-poll trong `duration_seconds` — mỗi khi user gõ 1
    trong các `/command`, bot tự trả lời text tương ứng. Kết thúc trả stats.
      * `commands` = {"/start":"Xin chào!", "/help":"...", "/ping":"pong"}.
      * `fallback_reply` = trả khi user gõ text không khớp command nào ("" = im lặng).
    Dùng cho use-case đơn giản; logic phức tạp → wait_for_reply + tự dispatch."""
    import time
    deadline = time.time() + max(1, duration_seconds)
    offset = 0
    handled = 0
    unmatched = 0
    log = []
    while time.time() < deadline:
        remaining = max(1, int(deadline - time.time()))
        r = _api("getUpdates", offset=offset or None, timeout=min(remaining, 25), limit=50)
        updates = r if isinstance(r, list) else []
        _remember_chats_from_updates(updates)
        for u in updates:
            offset = u["update_id"] + 1
            m = u.get("message") or {}
            txt = (m.get("text") or "").strip()
            cid = (m.get("chat") or {}).get("id")
            if not (txt and cid):
                continue
            cmd = txt.split()[0].split("@")[0]  # "/start@BotName foo" → "/start"
            reply = commands.get(cmd)
            if reply is None and fallback_reply:
                reply = fallback_reply
                unmatched += 1
            elif reply is not None:
                handled += 1
            if reply:
                _api("sendMessage", chat_id=cid, text=reply)
                log.append({"chat_id": cid, "cmd": cmd, "replied": True})
    return {
        "duration": duration_seconds,
        "handled": handled,
        "unmatched_but_replied": unmatched,
        "log_tail": log[-20:],
    }


# ─── Ingest media user sent TO the bot ───────────────────────────────────────

@mcp.tool()
def download_incoming_media(save_dir: str = "", chat_id: int = 0,
                            timeout_seconds: int = 30, kinds: list = None) -> dict:
    """Poll trong `timeout_seconds` để bắt file/photo/video/audio user gửi cho
    bot, tải về `save_dir` (default = %TEMP%). `kinds` = filter subset:
    ["document","photo","video","audio","voice","video_note","animation"] (None
    = tất cả). Trả list files đã lưu + số updates đã xem."""
    import time, tempfile
    kinds = set(kinds or ["document", "photo", "video", "audio", "voice",
                          "video_note", "animation"])
    target = Path(save_dir).expanduser().resolve() if save_dir else Path(tempfile.gettempdir())
    target.mkdir(parents=True, exist_ok=True)

    deadline = time.time() + max(1, timeout_seconds)
    offset = 0
    saved = []
    scanned = 0
    while time.time() < deadline:
        remaining = max(1, int(deadline - time.time()))
        r = _api("getUpdates", offset=offset or None, timeout=min(remaining, 25), limit=50)
        updates = r if isinstance(r, list) else []
        _remember_chats_from_updates(updates)
        for u in updates:
            offset = u["update_id"] + 1
            scanned += 1
            m = u.get("message") or u.get("edited_message") or {}
            cid = (m.get("chat") or {}).get("id")
            if chat_id and cid != chat_id:
                continue
            hits = []
            if "document" in kinds and m.get("document"):
                hits.append(("document", m["document"]))
            if "photo" in kinds and m.get("photo"):
                hits.append(("photo", m["photo"][-1]))  # largest resolution
            if "video" in kinds and m.get("video"):
                hits.append(("video", m["video"]))
            if "audio" in kinds and m.get("audio"):
                hits.append(("audio", m["audio"]))
            if "voice" in kinds and m.get("voice"):
                hits.append(("voice", m["voice"]))
            if "video_note" in kinds and m.get("video_note"):
                hits.append(("video_note", m["video_note"]))
            if "animation" in kinds and m.get("animation"):
                hits.append(("animation", m["animation"]))
            for kind, meta in hits:
                fid = meta.get("file_id")
                orig = meta.get("file_name") or f"{kind}_{fid[:12]}.bin"
                dl = download_file(fid, str(target / orig))
                if "error" not in dl:
                    saved.append({
                        "kind": kind,
                        "chat_id": cid,
                        "path": dl["path"],
                        "size": dl["size"],
                        "original_name": orig,
                        "message_id": m.get("message_id"),
                    })
    return {"scanned_updates": scanned, "saved_count": len(saved), "files": saved}


@mcp.tool()
def reply_to_message(chat_id: str, message_id: int, text: str,
                     parse_mode: str = "", reply_markup: dict = None) -> dict:
    """Shortcut: send_message với `reply_to_message_id` sẵn."""
    return _api(
        "sendMessage",
        chat_id=chat_id, text=text,
        parse_mode=parse_mode or None,
        reply_to_message_id=message_id,
        reply_markup=reply_markup or None,
    )


# ─── Payments (Telegram Payments 2.0) ─────────────────────────────────────────

@mcp.tool()
def send_invoice(chat_id: str, title: str, description: str, payload: str,
                 provider_token: str, currency: str, prices: list,
                 photo_url: str = "", need_name: bool = False,
                 need_phone_number: bool = False, need_email: bool = False) -> dict:
    """Gửi invoice thanh toán. `prices` = [{"label":"Product","amount":10000}]
    (amount = giá * 100 với USD/EUR, không nhân với JPY/…). `provider_token` từ
    @BotFather → Payments. Currency 3-letter ISO. Xem Bot API docs cho fields
    đầy đủ; dùng raw_api("sendInvoice", {...}) cho case phức tạp."""
    return _api(
        "sendInvoice",
        chat_id=chat_id, title=title, description=description, payload=payload,
        provider_token=provider_token, currency=currency, prices=prices,
        photo_url=photo_url or None,
        need_name=need_name or None,
        need_phone_number=need_phone_number or None,
        need_email=need_email or None,
    )


@mcp.tool()
def answer_pre_checkout_query(pre_checkout_query_id: str, ok: bool = True,
                              error_message: str = "") -> dict:
    """Trả lời pre-checkout (bắt buộc trong 10s sau khi user tap Pay).
    `ok=False` → phải kèm `error_message`."""
    return {"ok": bool(_api(
        "answerPreCheckoutQuery",
        pre_checkout_query_id=pre_checkout_query_id,
        ok=ok, error_message=error_message or None,
    ))}


@mcp.tool()
def answer_shipping_query(shipping_query_id: str, ok: bool = True,
                          shipping_options: list = None,
                          error_message: str = "") -> dict:
    """Trả lời shipping query (nếu invoice bật `need_shipping_address`).
    `shipping_options` = list of {id, title, prices:[{label,amount}]}."""
    return {"ok": bool(_api(
        "answerShippingQuery",
        shipping_query_id=shipping_query_id, ok=ok,
        shipping_options=shipping_options or None,
        error_message=error_message or None,
    ))}


@mcp.tool()
def raw_api(method: str, params: dict = None) -> dict:
    """Escape hatch — gọi tuỳ ý bất kỳ Telegram Bot API method nào (kể cả cái
    bridge chưa wrap). Ví dụ: raw_api("getStickerSet", {"name":"HotCherry"}).
    KHÔNG upload file được — chỉ JSON body."""
    return _api(method, **(params or {}))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        try:
            print(f"[tg_bot_mcp] token len: {len(_load_bot_token())}", file=sys.stderr)
            me = _api("getMe")
            print(f"[tg_bot_mcp] getMe: {me}", file=sys.stderr)
        except Exception as exc:
            print(f"[tg_bot_mcp] status failed: {exc}", file=sys.stderr)
        sys.exit(0)
    print("[tg_bot_mcp] starting stdio server", file=sys.stderr)
    mcp.run()
