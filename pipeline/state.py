"""Checkpoint state: tiny JSON, persisted across runs via Actions cache."""
import json

from . import config


def blank():
    return {
        "book_id": None, "phase": "idle", "tg_offset": 0,
        "title": "", "author": "", "minutes": config.DEFAULT_MINUTES,
        "tg_file_id": "",
        "file_name": "", "queue": [], "noticed": [],
        "chunks": [], "chunks_done": {}, "script_chars": 0,
        "chunk_chars": config.CHUNK_CHARS,
        "synth": {"done": [], "parts_sent": []},
        "fail_log": {}, "stuck": False, "cycles_failed": 0,
    }


def load():
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    if config.STATE_FILE.exists():
        try:
            s = blank()
            s.update(json.loads(config.STATE_FILE.read_text(encoding="utf-8")))
            return s
        except Exception:
            pass
    return blank()


def save(s):
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    config.STATE_FILE.write_text(json.dumps(s, indent=1), encoding="utf-8")
