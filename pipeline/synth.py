"""Fish Audio TTS (sequential, idempotent) + per-part m4b finalization.
Ports the proven company-of-one pattern: sec mp3s -> concat -> FFMETADATA -> m4b."""
import json
import pathlib
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error

from . import config

TTS_TRIES, TTS_WAITS = 5, [60, 120, 300, 600]


def find_ffmpeg():
    ff = shutil.which("ffmpeg")
    probe = shutil.which("ffprobe")
    if not ff or not probe:
        raise RuntimeError("ffmpeg/ffprobe not found on PATH")
    return ff, probe


def split_sections(md):
    parts = re.split(r"(?m)^#{1,2} ", md)
    secs = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        h, b = (p.split("\n", 1) + [""])[:2]
        secs.append((h.strip(), b.strip()))
    # guard: absurdly large single sections pre-split at paragraph breaks
    fixed = []
    for h, b in secs:
        if len(b) <= config.MAX_SECTION_CHARS:
            fixed.append((h, b))
            continue
        paras, cur = b.split("\n"), []
        for para in paras:
            cur.append(para)
            if sum(map(len, cur)) > config.MAX_SECTION_CHARS // 2:
                fixed.append((h + " (cont.)", "\n".join(cur)))
                cur = []
        if cur:
            fixed.append((h + " (cont.)", "\n".join(cur)))
    return fixed


_JUNK_HEADING = re.compile(r"(?i)^(page|part|section)\W*[0-9ivxlc]+\s*$|^0+[0-9]{2,}\s*$")


def _spoken(heading, body):
    """Text actually sent to TTS. Junk headings (page-split artifacts like
    'Page 0001') and synthetic '(cont.)' sections stay silent; real chapter
    titles ('Chapter 3', real names) are spoken as chapter starts."""
    h = heading.strip()
    if h.endswith("(cont.)"):
        return body.strip()  # split-section continuation: heading already spoken
    if not h or _JUNK_HEADING.match(h):
        return body.strip()
    return (h + ". " + body.strip()).strip()


def _tts_call(text, out_path):
    text = re.sub(r"\s+", " ", text.replace("#", "").replace("*", "")
                  .replace("_", "").replace("`", "").replace(">", " ")).strip()
    payload = {"text": text, "format": "mp3"}
    if config.FISH_VOICE_TS2:
        payload["reference_id"] = config.FISH_VOICE_TS2
    req = urllib.request.Request(
        "https://api.fish.audio/v1/tts", data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + config.FISH_API_KEY,
                 "Content-Type": "application/json", "model": config.FISH_MODEL})
    with urllib.request.urlopen(req, timeout=600) as r:
        pathlib.Path(out_path).write_bytes(r.read())


def synth_section(workdir, idx, heading, body):
    """Synthesize one section (skip if already on disk). Returns mp3 path."""
    f = workdir / f"sec{idx}.mp3"
    if f.exists() and f.stat().st_size > 1000:
        return f
    text = _spoken(heading, body)
    last = ""
    for a in range(TTS_TRIES):
        try:
            _tts_call(text, f)
            if f.stat().st_size > 1000:
                print(f"sec{idx}: {len(body)} chars -> {f.stat().st_size} bytes")
                return f
            last = "empty response"
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            print(f"sec{idx}: {last}, attempt {a + 1}/{TTS_TRIES}")
        except Exception as e:
            last = str(e)[:100]
            print(f"sec{idx}: {last}, attempt {a + 1}/{TTS_TRIES}")
        if a < len(TTS_WAITS):
            time.sleep(TTS_WAITS[a])
    raise RuntimeError(f"TTS stuck on sec{idx}: {last}")


def _esc(s):
    """Escape FFMETADATA values (= ; # newline and backslash are control chars)."""
    return (s.replace("\\", "\\\\").replace("=", "\\=").replace(";", "\\;")
             .replace("#", "\\#").replace("\n", "\\\n"))


def probe_dur(ffprobe, path):
    r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return float(r.stdout.strip())


def finalize_part(workdir, sec_idxs, headings, title, album, tag):
    """Concat sec mp3s -> chaptered m4b. Returns (m4b_path, minutes).
    tag names the files (caller numbers parts at send time)."""
    ff, probe = find_ffmpeg()
    files = [workdir / f"sec{i}.mp3" for i in sec_idxs]
    mixed = workdir / f"mix-{tag}.mp3"
    mixed.write_bytes(b"".join(f.read_bytes() for f in files))
    durs = [probe_dur(probe, f) for f in files]
    meta = [";FFMETADATA1", f"title={_esc(title)}", f"album={_esc(album)}"]
    t = 0
    for h, dur in zip(headings, durs):
        meta += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(t * 1000)}",
                 f"END={int((t + dur) * 1000)}", f"title={_esc(h[:120])}"]
        t += dur
    (workdir / f"ch-{tag}.txt").write_text("\n".join(meta) + "\n")
    m4b = workdir / f"part-{tag}.m4b"
    r = subprocess.run([ff, "-y", "-i", str(mixed),
                        "-i", str(workdir / f"ch-{tag}.txt"),
                        "-map_metadata", "1", "-c:a", "aac", "-b:a", "128k",
                        str(m4b)], capture_output=True, text=True)
    if not m4b.exists() or r.returncode != 0:
        raise RuntimeError(f"m4b finalize failed: {r.stderr[-300:]}")
    mixed.unlink(missing_ok=True)
    return m4b, sum(durs) / 60
