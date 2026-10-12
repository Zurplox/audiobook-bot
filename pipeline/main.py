"""Orchestrator: poll -> extract -> vision -> summarize -> deliver.
Every phase checkpoints; a crashed run resumes mid-book. Exit 0 always
(stuck runs alert via Telegram and continue on the next cycle)."""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

from . import config, state, tg, extract, vision, summarize, synth, deliver


def parse_minutes(caption):
    m = re.search(r"\d+", caption or "")
    if not m:
        return config.DEFAULT_MINUTES
    return max(config.MIN_MINUTES, min(config.MAX_MINUTES, int(m.group())))


def poll(s):
    """Consume Telegram updates: start/queue epubs, notice bad files, advance offset."""
    changed = False
    for u in tg.get_updates(s["tg_offset"]):
        s["tg_offset"] = u["update_id"] + 1
        changed = True
        try:
            handle_update(s, u)
        except Exception as e:
            print(f"update {u['update_id']} failed ({e!r}) — skipped with notice")
            tg.send("⚠️ Couldn't read that file — skipped. Send the .epub again?")
    return changed


def handle_update(s, u):
    msg = u.get("message", {})
    doc = msg.get("document")
    if not doc:
        return  # plain text: silent skip
    fid = doc["file_id"]
    uniq = doc.get("file_unique_id", fid)
    name = doc.get("file_name", "")
    if not name.lower().endswith(".epub"):
        if uniq not in s["noticed"]:
            tg.send(f"📚 I only take .epub files — '{name}' skipped.")
            s["noticed"].append(uniq)
        return
    if doc.get("file_size", 0) / 1024 / 1024 > config.TG_MAX_DOWNLOAD_MB:
        tg.send(f"📦 '{name}' is over Telegram's 20 MB bot-download cap — "
                f"please send a smaller epub.")
        return
    entry = {"file_id": fid, "name": name,
             "caption": (msg.get("caption") or ""),
             "override": os.environ.get("MINUTES_OVERRIDE", "")}
    if s["book_id"] is None:
        start_book(s, entry)
    elif len(s["queue"]) < 5:
        s["queue"].append(entry)
        tg.send(f"📥 '{name}' queued — current book finishes first.")
    else:
        tg.send(f"⏳ Queue is full (5) — send '{name}' again later.")


def start_book(s, entry):
    book_id = entry["file_id"][-16:].replace("/", "").replace("+", "") or "book"
    wd = config.WORK_DIR / book_id
    if wd.exists():
        shutil.rmtree(wd)
    wd.mkdir(parents=True)
    ok, mb = tg.download_document(entry["file_id"], wd / "book.epub")
    if not ok:
        tg.send(f"⚠️ Could not download '{entry['name']}' — send it again.")
        return
    fresh = state.blank()
    mins = parse_minutes(entry["caption"])
    if not re.search(r"\d+", entry["caption"] or "") and entry.get("override", "").isdigit():
        mins = max(config.MIN_MINUTES, min(config.MAX_MINUTES, int(entry["override"])))
    fresh.update({"book_id": book_id, "phase": "downloaded",
                  "tg_offset": s["tg_offset"], "queue": s["queue"],
                  "noticed": s["noticed"][-20:],
                  "minutes": mins, "tg_file_id": entry["file_id"],
                  "file_name": entry["name"], "title": entry["name"]})
    s.clear(); s.update(fresh)
    tg.send(f"📖 Received '{entry['name']}' ({mb:.1f} MB) — "
            f"building a ~{s['minutes']} min audiobook. I'll send each part here as it's done.")
    print(f"started {book_id}: {entry['name']}, {s['minutes']} min")


def _save_chapters(wd, chapters):
    (wd / "chapters.json").write_text(json.dumps(
        [{"title": c["title"], "lines": c["lines"], "images": [
            {"n": i["n"], "ext": i["ext"], "caption": i["caption"],
             "context": i["context"]} for i in c["images"]]} for c in chapters],
        ensure_ascii=False), encoding="utf-8")


def ensure_work(s):
    """Disaster path: repo-state survived but work files are gone (cache wiped).
    Re-fetch the epub from Telegram and re-extract. Summaries already in state
    are reused (same chunk size -> same ids); phase resets so vision re-runs."""
    wd = config.WORK_DIR / s["book_id"]
    if (wd / "chapters.json").exists():
        return
    if not s.get("tg_file_id"):
        raise RuntimeError("work files missing and no Telegram file id — send the book again")
    wd.mkdir(parents=True, exist_ok=True)
    ok, _ = tg.download_document(s["tg_file_id"], wd / "book.epub")
    if not ok:
        raise RuntimeError("epub re-download failed — send the book again")
    meta, chapters = extract.extract(wd / "book.epub", wd / "images")
    _save_chapters(wd, chapters)
    s["phase"] = "extracted"
    tg.send("🧠 Checkpoint cache was wiped — re-fetched the book, resuming from saved summaries.")
    print(f"rebuilt work files for {s['book_id']}: {len(chapters)} chapters")


def _git(root, *args):
    return subprocess.run(["git", *args], capture_output=True, text=True, cwd=str(root))


def commit_state(s, root=None):
    """Disaster-recovery copy: commit state.json to the repo (Actions only).
    Cache stays primary. Single writer + fresh clone per run => no conflicts
    in practice; any failure is non-fatal (runner is disposable). Never raises."""
    try:
        if os.environ.get("GITHUB_ACTIONS") != "true":
            return
        root = pathlib.Path(root or config.ROOT)
        if not (root / "state" / "state.json").exists():
            return
        st = _git(root, "status", "--porcelain", "state/state.json")
        if "state" not in st.stdout:
            return  # nothing new (covers both `?? state/` and ` M state/state.json`)
        _git(root, "pull", "--rebase", "--autostash")
        _git(root, "add", "state/state.json")
        _git(root, "-c", "user.name=audiobook-bot", "-c", "user.email=bot@local",
             "commit", "-qm", f"checkpoint: {s.get('book_id') or 'idle'} {s.get('phase')}")
        p = _git(root, "-c", "user.name=audiobook-bot", "-c", "user.email=bot@local",
                 "push", "origin", "HEAD:main")
        print("checkpoint commit:", "pushed" if p.returncode == 0 else p.stderr[-120:])
    except Exception as e:
        print("checkpoint commit skipped:", str(e)[:120])


def run_phases(s):
    wd = config.WORK_DIR / s["book_id"]
    if s["phase"] == "downloaded":
        meta, chapters = extract.extract(wd / "book.epub", wd / "images")
        s["title"] = meta.get("title") or s["file_name"].rsplit(".", 1)[0]
        s["author"] = meta.get("creator", "")
        _save_chapters(wd, chapters)
        s["phase"] = "extracted"
        print(f"extracted {len(chapters)} chapters")
    if s["phase"] == "extracted":
        tg.send("👁️ Reading %d figures for verbal description…"
                % sum(len(c["images"]) for c in
                      json.loads((wd / "chapters.json").read_text(encoding="utf-8"))))
        raw = json.loads((wd / "chapters.json").read_text(encoding="utf-8"))
        chapters = [{"title": c["title"], "lines": c["lines"],
                     "images": [{"n": i["n"], "ext": i["ext"], "caption": i["caption"],
                                 "context": i["context"]} for i in c["images"]]}
                    for c in raw]
        chapters = vision.enrich(chapters, wd / "images")
        (wd / "chapters.json").write_text(json.dumps(chapters, ensure_ascii=False),
                                          encoding="utf-8")
        s["phase"] = "vision_done"
    if s["phase"] == "vision_done":
        tg.send("🧠 Summarization starting (target ~%d min)…" % s["minutes"])
        chapters = json.loads((wd / "chapters.json").read_text(encoding="utf-8"))
        for ev in summarize.summarize(s, chapters, wd):
            state.save(s)
            print("progress:", ev)
        s["phase"] = "summarized"
    if s["phase"] == "summarized":
        tg.send("🎙️ Script ready — synthesizing audio, part 1 coming next…")
        md = (wd / "script.md").read_text(encoding="utf-8-sig")
        sections = synth.split_sections(md)
        print(len(sections), "TTS sections")
        for ev in deliver.deliver(s, wd, sections):
            state.save(s)
            print("progress:", ev)
        s["phase"] = "done"


def finish(s):
    parts = s["synth"]["parts_sent"]
    total = sum(p["mins"] for p in parts)
    tg.send(f"✅ '{s['title']}' complete — {len(parts)} part(s), {total:.0f} min total.")
    wd = config.WORK_DIR / s["book_id"]
    shutil.rmtree(wd, ignore_errors=True)
    done_name = s["file_name"]
    queue = [e for e in s["queue"] if e.get("name") != done_name]
    if len(queue) != len(s["queue"]):
        tg.send(f"🧹 Skipped duplicate upload of '{done_name}' — you already have it.")
    nxt = state.blank()
    nxt.update({"tg_offset": s["tg_offset"], "queue": queue,
                "noticed": s["noticed"][-20:]})
    s.clear(); s.update(nxt)
    if s["queue"]:
        start_book(s, s["queue"].pop(0))


def alert_stuck(s, reason):
    s["stuck"] = True
    s["cycles_failed"] = s.get("cycles_failed", 0) + 1
    fails = s.get("fail_log", {})
    detail = ", ".join(f"{k}: {v} fails" for k, v in fails.items()) or reason
    msg = (f"⚠️ '{s['title']}' stuck at phase {s['phase']} — {detail}. "
           f"State saved, auto-resume on next cycle (stuck x{s['cycles_failed']}).")
    if s["cycles_failed"] >= config.STUCK_ALERT_CYCLES:
        msg += " Manual look advised."
    tg.send(msg)
    print(msg)


def main():
    s = state.load()
    try:
        print("health:", end=" ")
        from . import llm
        llm.health_check()  # log only — the chain itself handles failures
        poll(s)
        state.save(s)
        if s["book_id"] is None:
            print("idle, offset", s["tg_offset"])
            return
        ensure_work(s)  # no-op unless cache was wiped mid-book
        try:
            run_phases(s)
            s["stuck"] = False; s["cycles_failed"] = 0
            state.save(s)
            if s["phase"] == "done":
                finish(s)
                state.save(s)
        except Exception as e:
            print("STUCK:", str(e)[:300])
            alert_stuck(s, str(e)[:200])
            state.save(s)
    finally:
        commit_state(s)  # repo copy of checkpoints (cache stays primary)


if __name__ == "__main__":
    main()
