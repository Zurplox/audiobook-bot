"""Map-reduce summarization.
Map: chapter chunks -> chunk summaries (checkpointed per chunk).
Reduce: per-chapter assembly (small outputs any fallback model can emit).
Open/close need no LLM calls: cold open = first chapter starts immediately,
closing = fixed template. No Harvin analogies in the auto pipeline."""
import json
import re

from . import config, extract, llm, tg

MAP_SYS = ("You condense nonfiction book chapters for an audiobook summary. "
           "Simple plain language, easy to follow by ear. Keep the author's key "
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


def build_chunks(chapters):
    chunks = []
    for ci, ch in enumerate(chapters):
        for c in extract.chunks_from_lines(ch["lines"], config.CHUNK_CHARS):
            chunks.append({"id": f"c{len(chunks)}", "ch": ci, "text": c})
    return chunks


def summarize(state, chapters, workdir):
    """Runs map + reduce with per-unit checkpoints in state. Returns script md."""
    chunks = build_chunks(chapters)
    if not state["chunks"] or len(state["chunks"]) != len(chunks):
        state["chunks"] = [{"id": c["id"], "ch": c["ch"]} for c in chunks]
        state["chunks_done"] = {}
    by_id = {c["id"]: c for c in chunks}

    for c in chunks:
        if c["id"] in state["chunks_done"]:
            continue
        words = max(120, len(c["text"]) // 12)
        msgs = [{"role": "system", "content": MAP_SYS},
                {"role": "user", "content": _map_prompt(c["text"], words)}]
        out, rung, fails = llm.call_chain(
            msgs, max_tokens=config.MAP_MAX_TOKENS,
            min_len=max(config.MAP_MIN_ABS, len(c["text"]) // config.MAP_MIN_RATIO),
            tag=f"map-{c['id']}", notified=state.setdefault("notified_rungs", []))
        state["chunks_done"][c["id"]] = out
        state["fail_log"] = fails
        if config.CHUNK_SPAM and fails:
            detail = " → ".join(f"{k}×{v}" for k, v in fails.items()) + f" → {rung} ok"
            tg.send(f"🧩 map-{c['id']}: {detail}")
        yield ("map", c["id"], rung)
        done_n = len(state["chunks_done"])
        if done_n % 5 == 0 or done_n == len(chunks):
            tg.send(f"🧠 Summarizing — {done_n}/{len(chunks)} chunks done")

    # reduce per chapter
    narrations = {}
    for ci, ch in enumerate(chapters):
        key = f"ch{ci}"
        if key in state.get("chapters_done", {}):
            narrations[ci] = state["chapters_done"][key]
            continue
        parts = [state["chunks_done"][c["id"]] for c in chunks if c["ch"] == ci]
        src = "\n\n".join(parts)
        msgs = [{"role": "system", "content": REDUCE_SYS},
                {"role": "user", "content":
                 f"Turn these summaries of the chapter '{ch['title']}' into flowing "
                 f"narration:\n\n{src}"}]
        out, rung, fails = llm.call_chain(
            msgs, max_tokens=config.REDUCE_MAX_TOKENS,
            min_len=max(400, len(src) // 8), tag=f"reduce-{key}",
            notified=state.setdefault("notified_rungs", []))
        state.setdefault("chapters_done", {})[key] = out
        state["fail_log"] = fails
        narrations[ci] = out
        if config.CHUNK_SPAM and fails:
            detail = " → ".join(f"{k}×{v}" for k, v in fails.items()) + f" → {rung} ok"
            tg.send(f"🧩 reduce-{key}: {detail}")
        yield ("reduce", key, rung)

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
