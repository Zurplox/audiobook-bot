"""Orchestrator: poll -> extract -> vision -> summarize -> deliver.
Every phase checkpoints; a crashed run resumes mid-book. Exit 0 always
(stuck runs alert via Telegram and continue on the next cycle)."""
import json
import os
import re
import shutil
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
        msg = u.get("message", {})
        doc = msg.get("document")
        if not doc:
            continue  # plain text: silent skip
        fid, uniq = doc["file_id"], doc.get("file_unique_id", fid)
        name = doc.get("file_name", "")
        if not name.lower().endswith(".epub"):
            if uniq not in s["noticed"]:
                tg.send(f"📚 I only take .epub files — '{name}' skipped.")
                s["noticed"].append(uniq)
            continue
        if doc.get("file_size", 0) / 1024 / 1024 > config.TG_MAX_DOWNLOAD_MB:
            tg.send(f"📦 '{name}' is over Telegram's 20 MB bot-download cap — "
                    f"please send a smaller epub.")
            continue
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
    return changed


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
                  "minutes": mins,
                  "file_name": entry["name"], "title": entry["name"]})
    s.clear(); s.update(fresh)
    tg.send(f"📖 Received '{entry['name']}' ({mb:.1f} MB) — "
            f"building a ~{s['minutes']} min audiobook. I'll send each part here as it's done.")
    print(f"started {book_id}: {entry['name']}, {s['minutes']} min")


def run_phases(s):
    wd = config.WORK_DIR / s["book_id"]
    if s["phase"] == "downloaded":
        meta, chapters = extract.extract(wd / "book.epub", wd / "images")
        s["title"] = meta.get("title") or s["file_name"].rsplit(".", 1)[0]
        s["author"] = meta.get("creator", "")
        (wd / "chapters.json").write_text(json.dumps(
            [{"title": c["title"], "lines": c["lines"], "images": [
                {"n": i["n"], "ext": i["ext"], "caption": i["caption"],
                 "context": i["context"]} for i in c["images"]]} for c in chapters],
            ensure_ascii=False), encoding="utf-8")
        s["phase"] = "extracted"
        print(f"extracted {len(chapters)} chapters")
    if s["phase"] == "extracted":
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
        chapters = json.loads((wd / "chapters.json").read_text(encoding="utf-8"))
        for ev in summarize.summarize(s, chapters, wd):
            state.save(s)
            print("progress:", ev)
        s["phase"] = "summarized"
    if s["phase"] == "summarized":
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
    nxt = state.blank()
    nxt.update({"tg_offset": s["tg_offset"], "queue": s["queue"],
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
    print("health:", end=" ")
    from . import llm
    llm.health_check()  # log only — the chain itself handles failures
    poll(s)
    state.save(s)
    if s["book_id"] is None:
        print("idle, offset", s["tg_offset"])
        return
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


if __name__ == "__main__":
    main()
