"""Shared config: secrets, endpoints, model chain, limits. Stdlib only."""
import os
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
STATE_DIR = ROOT / "state"
WORK_DIR = ROOT / "work"
STATE_FILE = STATE_DIR / "state.json"


def _load_dotenv():
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()


def env(name, default=""):
    return os.environ.get(name, default)


# --- secrets ---
FISH_API_KEY = env("FISH_API_KEY")
FISH_MODEL = env("FISH_MODEL", "s2.1-pro-free")
FISH_VOICE_TS2 = env("FISH_VOICE_TS2")
TG_TOKEN = env("TELEGRAM_BOT_TOKEN")
TG_CHAT = env("TELEGRAM_CHAT_ID")
NIM_API_KEY = env("NIM_API_KEY")
NIM_API_KEY_2 = env("NIM_API_KEY_2")
KILO_API_KEY = env("KILO_API_KEY")
OR_API_KEY = env("OPENROUTER_API_KEY")

# --- endpoints ---
NIM_BASE = "https://integrate.api.nvidia.com/v1"
KILO_BASE = "https://api.kilo.ai/api/gateway"
OR_BASE = "https://openrouter.ai/api/v1"

# --- models (pinned, smoke-tested 2026-10-11) ---
GLM_MODEL = "z-ai/glm-5.3-flash"
KILO_MODEL = "kilo-auto/free"            # auto-router over free models, 256k ctx
DS_MODEL = "deepseek-ai/deepseek-v4.1-flash"
OR_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
VISION_MODEL = "meta/llama-3.2-11b-vision-instruct"
KILO_VISION_MODEL = "dots-studio/dots-3-note-preview:free"  # verified w/ key 2026-10-11

# Summarize fallback chain: (name, base, model, key, tries, waits_between_secs)
# Order matters: quota-type errors jump to the next rung (never same-pool key).
CHAIN = [
    ("nim-glm", NIM_BASE, GLM_MODEL, NIM_API_KEY, 5, [15, 30, 60, 120]),
    ("kilo", KILO_BASE, KILO_MODEL, KILO_API_KEY, 3, [20, 20]),
    ("nim-ds", NIM_BASE, DS_MODEL, NIM_API_KEY_2, 10, [180] * 9),
    ("or-nemo", OR_BASE, OR_MODEL, OR_API_KEY, 3, [30, 30]),
]

# --- pipeline limits ---
CHUNK_CHARS = 24000          # map chunk size (1M-ctx primary handles it easily)
MAP_WORKERS = 5              # parallel map calls (~2-3 req/min vs per-min caps)
REDUCE_WORKERS = 3           # parallel reduce calls (longer each)
VISION_WORKERS = 3           # parallel figure descriptions
CHARS_PER_MIN = 1080         # TS2 narration rate (measured)
DEFAULT_MINUTES = 120
MIN_MINUTES, MAX_MINUTES = 15, 240
MAP_MIN_RATIO = 10           # map summary must be >= len(chunk)/10 and >= 300 chars
MAP_MIN_ABS = 300
TG_MAX_UPLOAD_MB = 50        # Telegram bot hard cap
PART_MAX_MB = 40             # our target ceiling (headroom under the cap)
EST_BYTES_PER_CHAR = 1.0     # synth size estimate (conservative)
MAX_SECTION_CHARS = 60000    # bigger sections pre-split (guard, rarely triggers)
VISION_TRIES = 3
VISION_IMG_MAX = 1024        # downscale longest side
# Output budgets must fit reasoning trace + content: all chain models reason,
# and a starved budget returns EMPTY content (13h lesson 2026-10-11: 0<min_len
# on every rung). Map content need ~3.5k tokens max; reasoning takes ~3k.
MAP_MAX_TOKENS = 12000
REDUCE_MAX_TOKENS = 12000
TG_MAX_DOWNLOAD_MB = 20      # Telegram bot getFile cap
STUCK_ALERT_CYCLES = 3       # consecutive stuck cycles before "manual look" alert
# Troubleshoot mode: Telegram message per chunk that needed any retry/fallback.
# Silent on clean first-try chunks. Flip to False when done troubleshooting.
CHUNK_SPAM = True
