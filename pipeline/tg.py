"""Telegram I/O: text/file sending (ported from notify_telegram.py) + update polling."""
import json
import pathlib
import urllib.request
import urllib.error
import uuid

from . import config

TG_API = f"https://api.telegram.org/bot{config.TG_TOKEN}"


def _configured():
    return bool(config.TG_TOKEN and config.TG_CHAT)


def _call(req, timeout=15):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        return True
    except urllib.error.HTTPError as e:
        print(f"Telegram HTTP {e.code}: {e.read()[:200]}")
        return False
    except Exception as e:
        print(f"Telegram error: {e}")
        return False


def send(text):
    if not _configured():
        print("Telegram not configured"); return False
    payload = json.dumps({"chat_id": config.TG_CHAT, "text": text}).encode()
    req = urllib.request.Request(TG_API + "/sendMessage", data=payload,
                                 headers={"Content-Type": "application/json"})
    return _call(req)


def send_document(path, caption=""):
    """Upload a file (bot cap 50 MB — caller keeps parts under PART_MAX_MB)."""
    if not _configured():
        return False
    p = pathlib.Path(path)
    if not p.exists():
        print(f"File not found: {p}"); return False
    size_mb = p.stat().st_size / 1024 / 1024
    if size_mb > config.TG_MAX_UPLOAD_MB:
        print(f"{size_mb:.1f} MB over Telegram cap — text fallback (BUG if reached)")
        return send(caption or f"Audiobook ready: {p.name}")
    boundary = uuid.uuid4().hex
    body = bytearray()

    def field(name, value):
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())

    field("chat_id", str(config.TG_CHAT))
    if caption:
        field("caption", caption)
    body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="document"; '
                f'filename="{p.name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode())
    body.extend(p.read_bytes())
    body.extend(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(TG_API + "/sendDocument", data=bytes(body),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    return _call(req, timeout=300)


def get_updates(offset, limit=20):
    """Return list of update dicts. Empty list on error (never raises)."""
    if not _configured():
        return []
    url = f"{TG_API}/getUpdates?offset={offset}&limit={limit}&timeout=0"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            d = json.load(r)
        return d.get("result", []) if d.get("ok") else []
    except Exception as e:
        print(f"getUpdates error: {e}")
        return []


def download_document(file_id, dest):
    """Download a Telegram file to dest. Returns (ok, size_mb)."""
    try:
        with urllib.request.urlopen(f"{TG_API}/getFile?file_id={file_id}", timeout=30) as r:
            d = json.load(r)
        if not d.get("ok"):
            return False, 0
        fpath = d["result"]["file_path"]
        size = d["result"].get("file_size", 0)
        if size / 1024 / 1024 > config.TG_MAX_DOWNLOAD_MB:
            return False, size / 1024 / 1024
        url = f"https://api.telegram.org/file/bot{config.TG_TOKEN}/{fpath}"
        with urllib.request.urlopen(url, timeout=300) as r:
            pathlib.Path(dest).write_bytes(r.read())
        return True, size / 1024 / 1024
    except Exception as e:
        print(f"download error: {e}")
        return False, 0
