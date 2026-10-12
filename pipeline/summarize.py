"""Map-reduce summarization.
Map runs on a worker pool (independent read-only LLM calls); reduce per chapter
on a smaller pool. Order preserved by id at assembly. Checkpoints per unit.
Open/close need no LLM calls: cold open = first chapter starts immediately,
closing = fixed template. No Harvin analogies in the auto pipeline."""
import concurrent.futures as cf
import json
import re
import threading

from . import config, extract, llm, tg

_SHORT = {"nim-glm": "GLM", "kilo": "Kilo", "nim-ds": "DeepSeek", "or-nemo": "Nemotron"}

MAP_SYS = ("You condense nonfiction book chapters for an audiobook summary. "           "Simple plain language, easy to follow by ear. Keep the author's key "
           "ideas, examples and numbers in their original order. When you see a "
           "[FIGURE n: ...] marker, narrate it verbally in 1-3 sentences as part "
           "of the flow - never write 'see figure' and never drop it. No intro "
           "or outro for the chunk. Plain paragraphs only, no markdown headings.")


def _map_prompt(chunk_text, target_words):
    return (f"Summarize the following book-chapter passage in about {target_words} "
            f"words, following the system instructions:\n\n{chunk_text}")


REDUCE_SYS = ("You assemble chapter summaries into flowing audiobook narration. "
              "Simple plain language for listening. Keep figure narrations where "
              "they appear. Expressive delivery through words and punctuation "
              "((laughs), Wow., ... pauses for emphasis). Plain paragraphs only, "
              "no markdown headings, no intro, no closing sentence.")


def build_chunks(chapters, limit=None):
    chunks = []
    for ci, ch in enumerate(chapters):
        for c in extract.chunks_from_lines(ch["lines"], limit or config.CHUNK_CHARS):
            chunks.append({"id": f"c{len(chunks)}", "ch": ci, "text": c})
    return chunks


def summarize(state, chapters, workdir):
    """Runs map + reduce with per-unit checkpoints in state. Returns script md."""
    # In-flight books keep their original chunk size (else checkpoints wipe).
    cchars = state.get("chunk_chars", 10000)
    chunks = build_chunks(chapters, cchars)
    if not state["chunks"] or len(state["chunks"]) != len(chunks):
        state["chunks"] = [{"id": c["id"], "ch": c["ch"]} for c in chunks]
        state["chunks_done"] = {}
    state["chunk_chars"] = cchars
    lock = threading.Lock()
    pending = [c for c in chunks if c["id"] not in state["chunks_done"]]

    def do_map(c):
        words = max(120, len(c["text"]) // 12)
        msgs = [{"role": "system", "content": MAP_SYS},
                {"role": "user", "content": _map_prompt(c["text"], words)}]
        out, rung, fails = llm.call_chain(
            msgs, max_tokens=config.MAP_MAX_TOKENS,
            min_len=max(config.MAP_MIN_ABS, len(c["text"]) // config.MAP_MIN_RATIO),
            tag=f"map-{c['id']}", notified=state.setdefault("notified_rungs", []))
        with lock:
            state["chunks_done"][c["id"]] = out
            state["fail_log"] = fails
            rc = state.setdefault("rung_counts", {})
            rc[rung] = rc.get(rung, 0) + 1
            done_n, total = len(state["chunks_done"]), len(chunks)
            parts = " · ".join(f"{_SHORT.get(k, k)} {v}" for k, v in sorted(rc.items()))
        if config.CHUNK_SPAM and fails:
            detail = " → ".join(f"{k}×{v}" for k, v in fails.items()) + f" → {rung} ok"
            tg.send(f"🧩 map-{c['id']}: {detail}")
        tick = (f"🧠 Summarizing — {done_n}/{total} chunks ({parts})"
                if done_n % 20 == 0 or done_n == total else None)
        return (c["id"], rung, tick)

    with cf.ThreadPoolExecutor(max_workers=config.MAP_WORKERS) as ex:
        for fut in cf.as_completed([ex.submit(do_map, c) for c in pending]):
            cid, rung, tick = fut.result()
            if tick:
                tg.send(tick)
            yield ("map", cid, rung)

    # reduce per chapter (smaller pool — longer calls)
    def do_reduce(job):
        ci, ch = job
        key = f"ch{ci}"
        src = "\n\n".join(state["chunks_done"][c["id"]] for c in chunks if c["ch"] == ci)
        msgs = [{"role": "system", "content": REDUCE_SYS},
                {"role": "user", "content":
                 f"Turn these summaries of the chapter '{ch['title']}' into flowing "
                 f"narration:\n\n{src}"}]
        out, rung, fails = llm.call_chain(
            msgs, max_tokens=config.REDUCE_MAX_TOKENS,
            min_len=max(400, len(src) // 8), tag=f"reduce-{key}",
            notified=state.setdefault("notified_rungs", []))
        with lock:
            state.setdefault("chapters_done", {})[key] = out
            state["fail_log"] = fails
        if config.CHUNK_SPAM and fails:
            detail = " → ".join(f"{k}×{v}" for k, v in fails.items()) + f" → {rung} ok"
            tg.send(f"🧩 reduce-{key}: {detail}")
        return (ci, rung)

    jobs = [(ci, ch) for ci, ch in enumerate(chapters)
            if f"ch{ci}" not in state.get("chapters_done", {})]
    with cf.ThreadPoolExecutor(max_workers=config.REDUCE_WORKERS) as ex:
        for fut in cf.as_completed([ex.submit(do_reduce, j) for j in jobs]):
            ci, rung = fut.result()
            yield ("reduce", f"ch{ci}", rung)

    narrations = {ci: state["chapters_done"][f"ch{ci}"] for ci in range(len(chapters))}

    title, author = state["title"], state["author"]
    md = [f"# {title} — {author}".strip(" —"), ""]
    for ci, ch in enumerate(chapters):
        md += [f"## {ch['title']}", "", narrations[ci].strip(), ""]
    md += ["## The last word", "",
           f"That is the heart of {title}. Take one idea from it and use it this week. "
           f"Small steps, repeated, is how every lasting change is built.", ""]
    script = "\n".join(md)
    (workdir / "script.md").write_text(script, encoding="utf-8")
    state["script_chars"] = len(script)
    target = state["minutes"] * config.CHARS_PER_MIN
    print(f"script {len(script)} chars vs target ~{target} ({len(script)/target:.0%})")
