"""Google OAuth helper for connect-AI — ONE Google sign-in covers every Google
surface in the workspace (Gmail + Calendar + Drive).

The rule this module enforces: a Google account is connected once, and that
single login is what every `google_*` connector runs on. There is no
per-connector token paste, no per-service login. Signing in connects all three
connectors; signing out revokes the token at Google and disconnects all three.

The refresh_token comes from the 1-click consent flow (helper 8766
`/google/auth-start` → `/google/auth-callback`) or, as a fallback, from a value
the user pastes after doing the OAuth Playground flow with Access type =
Offline. It is stored in `google-tokens.json` at the project root. Every
subsequent launch, this module refreshes the access_token in a background
thread and pushes the fresh token into the runtime's gmail / google_calendar /
google_drive connectors, so the agent never sees a 401.

Playground's client credentials are used because the refresh_token is
issued to whichever client asked for it — refreshing against a different
client will fail. Playground's client_secret is public knowledge
(embedded in the Playground JavaScript for years). If Google ever
revokes or rotates it, we'll surface a clear error and the user can drop
their own OAuth Desktop client into `google-oauth.json` to override.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
TOKEN_FILE = ROOT / "google-tokens.json"
OWN_CREDS_FILE = ROOT / "google-oauth.json"
DEFAULT_API_TOKEN = os.environ.get("CONNECT_AI_API_TOKEN") or os.environ.get(
    "COWORKER_API_TOKEN", "connect-ai-dev-token"
)

# Playground defaults — public knowledge, used when the user has no own
# OAuth Desktop client. Refreshing works because our refresh_token was
# issued by this exact client (which is what Playground uses too).
PLAYGROUND_CLIENT_ID = "407408718192.apps.googleusercontent.com"
PLAYGROUND_CLIENT_SECRET = "d-FL95Q19q7MQmFpd7hHD0Ty"  # noqa: S105

TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v3/userinfo"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"

# Every Google-backed connector in the sidecar. One sign-in feeds all of them —
# add a connector here and it joins the same login, no new credential path.
CONNECTORS = ("gmail", "google_calendar", "google_drive")
CONNECTOR_TITLES = {
    "gmail": "Gmail",
    "google_calendar": "Google Calendar",
    "google_drive": "Google Drive",
}

# One consent screen granting everything the three connectors need. Keep this in
# sync with CONNECTORS: a scope missing here means that connector 401s later.
DEFAULT_SCOPES = [
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/calendar",
]

REFRESH_INTERVAL_SEC = 50 * 60  # 50 min, comfortably before 60-min expiry


def _load_client() -> tuple[str, str]:
    """Prefer a user-supplied Desktop OAuth client if `google-oauth.json`
    exists (dropped from Google Cloud Console); otherwise fall back to
    Playground's credentials.
    """
    if OWN_CREDS_FILE.exists():
        raw = json.loads(OWN_CREDS_FILE.read_text(encoding="utf-8"))
        # Google's downloaded creds file wraps under "installed" or "web".
        node = raw.get("installed") or raw.get("web") or raw
        cid = node.get("client_id")
        secret = node.get("client_secret")
        if cid and secret:
            return cid, secret
    return PLAYGROUND_CLIENT_ID, PLAYGROUND_CLIENT_SECRET


def _load_tokens() -> dict:
    """Read the token store. Auto-migrates the old flat schema
      {"refresh_token": "...", "access_token": "...", "expires_at": ...}
    into the multi-account schema
      {"accounts": {"default": {...same fields...}}, "active": "default"}
    so callers can always assume the new shape without breaking existing files."""
    if not TOKEN_FILE.exists():
        return {"accounts": {}, "active": ""}
    raw = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    if "accounts" in raw and isinstance(raw["accounts"], dict):
        raw.setdefault("active", next(iter(raw["accounts"]), ""))
        return raw
    # Legacy flat file → migrate in place
    migrated = {"accounts": {}, "active": ""}
    if raw.get("refresh_token"):
        migrated["accounts"]["default"] = raw
        migrated["active"] = "default"
    _save_tokens(migrated)
    return migrated


def _save_tokens(data: dict) -> None:
    TOKEN_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _resolve_account(store: dict, account: str = "") -> str:
    """Fall back to `active`, then the first available account, then 'default'."""
    if account:
        return account
    return store.get("active") or next(iter(store.get("accounts", {})), "default")


def list_accounts() -> list[dict]:
    """List every Google account currently stored — email, has-refresh-token,
    seconds until access_token expiry, active flag. No secrets exposed."""
    store = _load_tokens()
    active = store.get("active", "")
    out = []
    for name, entry in store.get("accounts", {}).items():
        out.append({
            "name": name,
            "email": entry.get("email") or entry.get("account"),
            "has_refresh_token": bool(entry.get("refresh_token")),
            "expires_in": (
                entry.get("expires_at", 0) - int(time.time())
                if entry.get("expires_at") else None
            ),
            "is_active": name == active,
        })
    return out


def remove_account(name: str) -> bool:
    """Delete an account from the store. If it was active, the next remaining
    account becomes active."""
    store = _load_tokens()
    if name not in store.get("accounts", {}):
        return False
    del store["accounts"][name]
    if store.get("active") == name:
        store["active"] = next(iter(store["accounts"]), "")
    _save_tokens(store)
    return True


def set_active_account(name: str) -> bool:
    """Point `active` at a specific account."""
    store = _load_tokens()
    if name not in store.get("accounts", {}):
        return False
    store["active"] = name
    _save_tokens(store)
    return True


def refresh_access_token(account: str = "") -> Optional[str]:
    """Exchange the stored refresh_token for a fresh access_token. When `account`
    is empty, uses the active account."""
    store = _load_tokens()
    name = _resolve_account(store, account)
    entry = store.get("accounts", {}).get(name) or {}
    rt = entry.get("refresh_token")
    if not rt:
        return None
    cid, secret = _load_client()
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": rt,
        "client_id": cid,
        "client_secret": secret,
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            TOKEN_ENDPOINT,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as res:
            data = json.loads(res.read())
        new_access = data.get("access_token")
        if not new_access:
            return None
        entry["access_token"] = new_access
        entry["expires_at"] = int(time.time()) + int(data.get("expires_in", 3600))
        store["accounts"][name] = entry
        _save_tokens(store)
        return new_access
    except Exception as exc:  # noqa: BLE001
        print(f"[google-auth] refresh({name}) failed: {exc}", file=sys.stderr)
        return None


# ─── OAuth callback flow (real Google consent → 127.0.0.1 redirect) ───────────

def build_authorization_url(
    redirect_uri: str,
    scopes: list[str] | None = None,
    state: str = "",
    login_hint: str = "",
) -> str:
    """Construct the Google OAuth consent URL. `redirect_uri` MUST be pre-
    registered on the OAuth client — for Desktop clients any http://127.0.0.1:PORT
    URI is accepted without pre-registration. Playground's web client will
    reject anything but its own URI, so callback mode requires the user to drop
    their own Desktop OAuth client into `google-oauth.json`."""
    cid, _ = _load_client()
    params = {
        "client_id": cid,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes or DEFAULT_SCOPES),
        "access_type": "offline",  # ← required to get a refresh_token
        # consent → Google always issues a refresh_token; select_account → the
        # chooser shows every time, which is how "add another account" works.
        "prompt": "consent select_account",
        "include_granted_scopes": "true",
    }
    if state:
        params["state"] = state
    if login_hint:
        params["login_hint"] = login_hint
    return AUTH_ENDPOINT + "?" + urllib.parse.urlencode(params)


def exchange_code_for_tokens(code: str, redirect_uri: str) -> dict:
    """POST /token to swap the one-time `code` for {access_token, refresh_token,
    expires_in, ...}. Returns the raw response dict; caller decides how to
    persist."""
    cid, secret = _load_client()
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "client_id": cid,
        "client_secret": secret,
        "redirect_uri": redirect_uri,
    }).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as res:
        return json.loads(res.read())


def _fetch_userinfo(access_token: str) -> dict:
    """Get the Google account's email + profile. Uses the `email profile openid`
    scopes we requested during consent."""
    try:
        req = urllib.request.Request(
            USERINFO_ENDPOINT,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as res:
            return json.loads(res.read())
    except Exception:
        return {}


def complete_oauth_callback(
    code: str,
    redirect_uri: str,
    account_hint: str = "",
    push: bool = True,
    api_base: str = "http://127.0.0.1:8765",
    api_token: str = DEFAULT_API_TOKEN,
) -> dict:
    """One-shot: exchange `code` → store tokens under a slot named after the
    Google email (falls back to `account_hint`, then "default"), push the fresh
    access_token to the runtime, and return {account, email, expires_in}."""
    data = exchange_code_for_tokens(code, redirect_uri)
    at = data.get("access_token")
    rt = data.get("refresh_token")
    if not at:
        return {"error": data.get("error_description") or data.get("error") or "no access_token"}
    if not rt:
        # Google withholds refresh_token if the user has already granted this
        # app before and we didn't force `prompt=consent`. build_authorization_url
        # forces it, so this branch is rare — but surface it clearly.
        return {"error": "no refresh_token in Google response — revoke prior consent at "
                         "https://myaccount.google.com/permissions and try again"}

    info = _fetch_userinfo(at)
    email = info.get("email") or account_hint or "default"
    name = email  # use email as the slot key so switching accounts is human-readable

    store = _load_tokens()
    store.setdefault("accounts", {})[name] = {
        "refresh_token": rt,
        "access_token": at,
        "expires_at": int(time.time()) + int(data.get("expires_in", 3600)),
        "email": email,
        "picture": info.get("picture"),
        "name_display": info.get("name"),
    }
    store["active"] = name
    _save_tokens(store)

    pushed: dict[str, str] = {}
    if push:
        # A fresh login always connects EVERY Google connector — that is the whole
        # point of "sign in with Google" here — and becomes the default account.
        pushed = push_to_connectors(at, api_base, api_token)
        _set_default_everywhere(email.strip().lower(), api_base, api_token)
    return {
        "account": name,
        "email": email,
        "expires_in": int(data.get("expires_in", 3600)),
        "connectors": pushed,
    }


def have_own_desktop_client() -> bool:
    """True if the user has dropped a Google Cloud Desktop OAuth client in
    google-oauth.json — required for the 127.0.0.1 callback flow (Playground's
    web client refuses non-Playground redirects)."""
    return OWN_CREDS_FILE.exists()


# ─── connect-AI runtime plumbing ──────────────────────────────────────────────

def _ow_call(
    method: str,
    path: str,
    api_base: str = "http://127.0.0.1:8765",
    api_token: str = DEFAULT_API_TOKEN,
    payload: Optional[dict] = None,
    timeout: int = 10,
) -> dict:
    """One call against the sidecar. Returns the parsed body, or {"error": …}."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"{api_base}{path}",
        data=data,
        headers={
            "Content-Type": "application/json",
            "x-connect-ai-token": api_token,
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read()
        return json.loads(raw) if raw else {}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def sidecar_google_state(
    api_base: str = "http://127.0.0.1:8765",
    api_token: str = DEFAULT_API_TOKEN,
) -> dict[str, dict]:
    """What the sidecar currently holds for each Google connector:
    {name: {"connected": bool, "accounts": [email, …], "title": str}}.

    Listing also runs the sidecar's lazy account migration (a token-bearing
    `<connector>:default` becomes `<connector>:account:<email>`), which is why
    push_to_connectors calls this right after writing credentials — otherwise a
    second account's connect would overwrite the first one's staging profile.
    """
    data = _ow_call("GET", "/v1/connectors", api_base, api_token)
    out: dict[str, dict] = {
        name: {"connected": False, "accounts": [], "title": CONNECTOR_TITLES[name]}
        for name in CONNECTORS
    }
    for entry in data.get("connectors") or []:
        name = entry.get("name")
        if name not in out:
            continue
        emails = [
            str(a.get("email") or a.get("account_id") or "").strip().lower()
            for a in (entry.get("accounts") or [])
        ]
        out[name] = {
            "connected": bool(entry.get("connected")),
            "accounts": [e for e in emails if e],
            "title": entry.get("title") or CONNECTOR_TITLES[name],
            "default_account": (entry.get("account") or "").strip().lower(),
        }
    return out


def push_to_connectors(
    access_token: str,
    api_base: str = "http://127.0.0.1:8765",
    api_token: str = DEFAULT_API_TOKEN,
    connectors: Optional[tuple[str, ...]] = None,
) -> dict[str, str]:
    """Feed one Google access_token into every Google connector — this is what
    makes a single sign-in cover Gmail, Calendar and Drive at once.

    Returns {connector: "ok" | error message}. Per-connector failures never abort
    the others (a Workspace account without Gmail still gets Drive + Calendar).
    """
    results: dict[str, str] = {}
    for name in connectors or CONNECTORS:
        # Skip disconnect — connect endpoint replaces credentials in place
        # and the disconnect+connect dance sometimes clobbers persona defaults.
        res = _ow_call(
            "POST",
            f"/v1/connectors/{name}/connect",
            api_base,
            api_token,
            {"fields": {"access_token": access_token}},
        )
        if res.get("ok"):
            results[name] = "ok"
        else:
            err = res.get("error") or "connect failed"
            results[name] = err
            print(f"[google-auth] push {name} failed: {err}", file=sys.stderr)
    # Force the sidecar's lazy per-account migration to run now (see docstring).
    sidecar_google_state(api_base, api_token)
    return results


def _set_default_everywhere(
    email: str,
    api_base: str = "http://127.0.0.1:8765",
    api_token: str = DEFAULT_API_TOKEN,
) -> None:
    """Point every Google connector's default account at the active login, so
    "which account does the agent use" has ONE answer across Gmail/Cal/Drive."""
    if not email:
        return
    for name in CONNECTORS:
        _ow_call(
            "POST",
            f"/v1/connectors/{name}/accounts/{urllib.parse.quote(email)}/default",
            api_base,
            api_token,
            {},
        )


def sync_all_accounts(
    api_base: str = "http://127.0.0.1:8765",
    api_token: str = DEFAULT_API_TOKEN,
    only_connected: bool = False,
) -> dict[str, dict[str, str]]:
    """Refresh EVERY stored Google account and push each one into the connectors.

    `only_connected=True` (the periodic refresher) keeps alive what the sidecar
    already holds instead of re-creating it: a connector — or a single account —
    the user disconnected in the GUI stays disconnected. The one exception is a
    sidecar with nothing Google connected at all: that's a first boot after
    login, so everything gets wired up.

    Returns {account: {connector: status}}; the active account is pushed last so
    it wins the default-account pointer.
    """
    store = _load_tokens()
    names = list(store.get("accounts", {}))
    if not names:
        return {}
    active = _resolve_account(store)
    ordered = [n for n in names if n != active] + [n for n in names if n == active]

    state = sidecar_google_state(api_base, api_token) if only_connected else {}
    fresh_sidecar = only_connected and not any(s["connected"] for s in state.values())

    out: dict[str, dict[str, str]] = {}
    for name in ordered:
        token = refresh_access_token(name)
        if not token:
            out[name] = {c: "refresh failed" for c in CONNECTORS}
            continue
        if only_connected and not fresh_sidecar:
            email = str(
                (store.get("accounts", {}).get(name) or {}).get("email") or name
            ).strip().lower()
            targets = tuple(
                c
                for c in CONNECTORS
                if state[c]["connected"] and email in state[c]["accounts"]
            )
            if not targets:
                out[name] = {}
                continue
        else:
            targets = CONNECTORS
        out[name] = push_to_connectors(token, api_base, api_token, connectors=targets)

    active_email = str(
        (store.get("accounts", {}).get(active) or {}).get("email") or active
    ).strip().lower()
    _set_default_everywhere(active_email, api_base, api_token)
    return out


# ─── Sign-out: one action removes Google everywhere ───────────────────────────

def _revoke_at_google(refresh_token: str) -> bool:
    """Tell Google to drop the grant, so signing out here really is signed out."""
    if not refresh_token:
        return False
    body = urllib.parse.urlencode({"token": refresh_token}).encode("utf-8")
    try:
        req = urllib.request.Request(
            REVOKE_ENDPOINT,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[google-auth] revoke failed: {exc}", file=sys.stderr)
        return False


def logout(
    account: str = "",
    api_base: str = "http://127.0.0.1:8765",
    api_token: str = DEFAULT_API_TOKEN,
) -> dict:
    """Sign out of Google — the mirror image of the 1-click login.

    Empty `account` signs out of every stored Google account (and every Google
    connector); a named account signs that one out of Gmail, Calendar AND Drive
    at once. Local tokens are deleted and the grant is revoked at Google.
    """
    store = _load_tokens()
    stored = store.get("accounts", {})
    targets = [account] if account else list(stored)
    if account and account not in stored:
        return {"ok": False, "error": f"account '{account}' not found"}

    emails: list[str] = []
    for name in targets:
        entry = stored.get(name) or {}
        _revoke_at_google(entry.get("refresh_token", ""))
        emails.append(str(entry.get("email") or name).strip().lower())
        remove_account(name)

    # Sidecar side: drop that account from each Google connector; when no login
    # is left at all, disconnect the connectors outright so nothing lingers.
    remaining = _load_tokens().get("accounts", {})
    for name in CONNECTORS:
        if remaining:
            for email in emails:
                _ow_call(
                    "POST",
                    f"/v1/connectors/{name}/accounts/{urllib.parse.quote(email)}/disconnect",
                    api_base,
                    api_token,
                    {},
                )
        else:
            _ow_call("POST", f"/v1/connectors/{name}/disconnect", api_base, api_token, {})

    if remaining:
        # Keep the default pointers valid after removing the previous default.
        active = _resolve_account(_load_tokens())
        active_email = str(
            (remaining.get(active) or {}).get("email") or active
        ).strip().lower()
        _set_default_everywhere(active_email, api_base, api_token)

    return {
        "ok": True,
        "signed_out": emails,
        "remaining": list(remaining),
        "connectors": list(CONNECTORS),
    }


def bootstrap_from_playground_paste(paste: str) -> bool:
    """First-run setup: user pastes the Step 2 JSON blob from OAuth Playground
    (the one shown under "Response" — has access_token + refresh_token).
    Alternatively, they can paste just the refresh_token string.
    """
    paste = paste.strip()
    if not paste:
        return False
    tokens = {}
    if paste.startswith("{"):
        try:
            data = json.loads(paste)
        except json.JSONDecodeError:
            return False
        tokens["refresh_token"] = data.get("refresh_token", "")
        tokens["access_token"] = data.get("access_token", "")
    elif paste.startswith("1//"):  # refresh_token prefix
        tokens["refresh_token"] = paste
    elif paste.startswith("ya29."):  # user pasted access_token instead
        print(
            "[google-auth] That looks like an access_token (ya29...). We need "
            "the refresh_token (starts with 1//) — turn on 'Access type: Offline' "
            "in Playground Settings, redo Step 1+2, and paste the whole JSON "
            "response from Step 2.",
            file=sys.stderr,
        )
        return False
    else:
        return False

    if not tokens.get("refresh_token"):
        print("[google-auth] no refresh_token found in paste", file=sys.stderr)
        return False

    store = _load_tokens()
    store.setdefault("accounts", {})["default"] = tokens
    store["active"] = "default"
    _save_tokens(store)
    fresh = refresh_access_token("default")
    if not fresh:
        print("[google-auth] refresh_token rejected by Google", file=sys.stderr)
        return False
    info = _fetch_userinfo(fresh)
    if info.get("email"):
        # Re-key the slot by email so it lines up with the 1-click login's slots.
        entry = store["accounts"].pop("default")
        entry["email"] = info["email"]
        entry["name_display"] = info.get("name")
        store["accounts"][info["email"]] = entry
        store["active"] = info["email"]
        _save_tokens(store)
    results = push_to_connectors(fresh)
    _set_default_everywhere(str(info.get("email") or "").lower())
    ok = [n for n, r in results.items() if r == "ok"]
    print(f"[google-auth] Bootstrapped — connected: {', '.join(ok) or 'none'} (auto-refresh on).")
    return True


def start_refresher(
    api_base: str = "http://127.0.0.1:8765",
    api_token: str = DEFAULT_API_TOKEN,
) -> Optional[threading.Thread]:
    """Kick off a daemon thread that refreshes + pushes on schedule — EVERY
    stored Google account, into every Google connector that account is signed
    into. Returns None (silently) if no account is stored yet."""
    store = _load_tokens()
    if not store.get("accounts"):
        return None

    def loop():
        # Refresh immediately so the sidecar has fresh tokens on boot.
        sync_all_accounts(api_base, api_token, only_connected=True)
        while True:
            time.sleep(REFRESH_INTERVAL_SEC)
            sync_all_accounts(api_base, api_token, only_connected=True)

    t = threading.Thread(target=loop, name="google-refresher", daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    # CLI: `python google_auth.py setup`       (interactive paste)
    #      `python google_auth.py connect-all` (re-wire Gmail+Calendar+Drive)
    #      `python google_auth.py refresh`     (one-shot refresh + push)
    #      `python google_auth.py logout [account]` (revoke + disconnect all)
    #      `python google_auth.py status`
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "setup":
        print("Paste the entire JSON response from OAuth Playground Step 2")
        print("(has access_token + refresh_token), then Ctrl+Z + Enter on Windows:")
        blob = sys.stdin.read()
        ok = bootstrap_from_playground_paste(blob)
        sys.exit(0 if ok else 1)
    elif cmd == "refresh":
        tok = refresh_access_token()
        if not tok:
            print("no refresh_token stored — run `setup` first", file=sys.stderr)
            sys.exit(1)
        results = push_to_connectors(tok)
        for name, status in results.items():
            print(f"  {name:18s} {status}")
    elif cmd in ("connect-all", "sync"):
        # Re-wire every Google connector from the stored login(s) — the fix for
        # "Drive says 401" without going through consent again.
        results = sync_all_accounts()
        if not results:
            print("no accounts connected — sign in first", file=sys.stderr)
            sys.exit(1)
        for account, per_connector in results.items():
            print(account)
            for name, status in per_connector.items():
                print(f"  {name:18s} {status}")
    elif cmd == "logout":
        target = sys.argv[2] if len(sys.argv) > 2 else ""
        result = logout(target)
        if not result.get("ok"):
            print(result.get("error") or "logout failed", file=sys.stderr)
            sys.exit(1)
        print(f"signed out: {', '.join(result['signed_out'])} (Gmail + Calendar + Drive)")
    elif cmd == "status":
        accounts = list_accounts()
        if not accounts:
            print("no accounts connected — POST to /google/auth-start or run `setup`")
        for a in accounts:
            marker = "★" if a["is_active"] else " "
            ttl = f"{a['expires_in']}s" if a["expires_in"] is not None else "n/a"
            print(f"{marker} {a['name']:30s}  email={a.get('email') or '-'}  refresh={a['has_refresh_token']}  ttl={ttl}")
        for name, state in sidecar_google_state().items():
            mark = "✓" if state["connected"] else "·"
            who = ", ".join(state["accounts"]) or "-"
            print(f"{mark} {state['title']:18s} {who}")
    elif cmd == "accounts":
        print(json.dumps(list_accounts(), indent=2, ensure_ascii=False))
    elif cmd == "activate":
        target = sys.argv[2] if len(sys.argv) > 2 else ""
        if not target or not set_active_account(target):
            print(f"failed to activate '{target}' — check `status` for available accounts", file=sys.stderr)
            sys.exit(1)
        tok = refresh_access_token(target)
        if tok:
            push_to_connectors(tok)
            _set_default_everywhere(target.strip().lower())
        print(f"active account is now: {target} (Gmail + Calendar + Drive)")
    elif cmd == "remove":
        # Same thing as `logout <account>` — removing a login locally while the
        # connectors still hold its token would just be a half-signed-out state.
        target = sys.argv[2] if len(sys.argv) > 2 else ""
        result = logout(target) if target else {"ok": False, "error": "account required"}
        if not result.get("ok"):
            print(result.get("error") or f"account '{target}' not found", file=sys.stderr)
            sys.exit(1)
        print(f"removed: {target}")
    else:
        print(
            "usage: python google_auth.py "
            "{setup|refresh|connect-all|status|accounts|activate <name>|remove <name>|logout [account]}",
            file=sys.stderr,
        )
        sys.exit(1)
