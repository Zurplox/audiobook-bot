"""Vision descriptions for kept figures. Primary: NIM llama-3.2-11b-vision.
Falls back per-image to tier-1 (caption/context) — vision never blocks the book."""
import base64
import concurrent.futures as cf
import io
import json
import time
import urllib.request
import urllib.error
from PIL import Image

from . import config

PROMPT = ("Describe this book figure in 2-4 plain sentences: what it shows, and "
          "the point it makes. Stay consistent with the provided context. "
          "Plain text only, no formatting, no preamble.")


def _call_vision(base, model, key, b64, text, max_tokens, timeout=180):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}}]}],
        "max_tokens": max_tokens}).encode()
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions", data=body,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    return (((d.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()


def _describe(img_bytes, caption, context):
    try:
        with Image.open(io.BytesIO(img_bytes)) as im:
            im = im.convert("RGB")
            im.thumbnail((config.VISION_IMG_MAX, config.VISION_IMG_MAX))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=70)
            b64 = base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        print(f"vision downscale failed: {e}")
        return ""
    ctx = " ".join(x for x in (caption, context) if x)
    text = PROMPT + (f"\nContext from the book: {ctx}" if ctx else "")
    # primary: NIM vision; fallback: Kilo vision (different provider/quota)
    rungs = [(config.NIM_BASE, config.VISION_MODEL, config.NIM_API_KEY, 300,
              config.VISION_TRIES),
             (config.KILO_BASE, config.KILO_VISION_MODEL, config.KILO_API_KEY, 1000, 2)]
    for base, model, key, maxtok, tries in rungs:
        if not key:
            continue
        for attempt in range(tries):
            try:
                out = _call_vision(base, model, key, b64, text, maxtok)
                if len(out) >= 40:
                    return out
                print(f"vision {model} short output ({len(out)}), retry {attempt + 1}")
            except urllib.error.HTTPError as e:
                print(f"vision {model} HTTP {e.code}, retry {attempt + 1}")
            except Exception as e:
                print(f"vision {model} error {str(e)[:120]}, retry {attempt + 1}")
            time.sleep(15)
    return ""


def enrich(chapters, img_dir):
    """Replace [FIGURE n ...] markers with vision descriptions (tier 2).
    Tier 1 (caption/context) stays when vision fails; both beat a dropped figure."""
    jobs = [(ch, im) for ch in chapters for im in ch["images"]]

    def do(job):
        ch, im = job
        try:
            p = img_dir / f"fig{im['n']}.{im['ext']}"
            desc = _describe(p.read_bytes(), im["caption"], im["context"]) if p.exists() else ""
        except Exception as e:
            print(f"fig{im['n']}: worker error {str(e)[:100]}")
            desc = ""
        return (im["n"], desc)

    descs = {}
    with cf.ThreadPoolExecutor(max_workers=config.VISION_WORKERS) as ex:
        for n, desc in ex.map(do, jobs):
            descs[n] = desc
    for ch in chapters:
        for im in ch["images"]:
            desc = descs.get(im["n"], "")
            if desc:
                im["desc"] = desc
                for i, ln in enumerate(ch["lines"]):
                    if ln.startswith(f"[FIGURE {im['n']}") or ln.startswith(f"[FIGURE {im['n']}:"):
                        ch["lines"][i] = f"[FIGURE {im['n']}: {desc}]"
                        break
                print(f"fig{im['n']}: vision {len(desc)} chars")
            else:
                print(f"fig{im['n']}: vision failed, keeping caption/context tier")
    return chapters
