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


CONDENSE_SYS = ("You compress chapter narrations to a strict length budget. "
                "Simple plain language for listening. Keep every key idea, example "
                "and number — rewrite tighter, never drop content and never truncate. "
                "Expressive delivery through words and punctuation. Plain paragraphs "
                "only, no markdown headings, no intro, no closing sentence.")


def _condense(state, parts, target):
    """Backstop: total over budget -> regroup narrations and re-summarize each
    group down to target share. Rewriting, never truncation. Checkpointed.
    Generator: yields progress events, returns the condensed parts list."""
    n = len(parts)
    G = min(config.CONDENSE_GROUPS, max(1, n))
    per_group = max(400, target // G)
    # balanced groups by chars
    groups, cur, curlen = [], [], 0
    want_each = sum(len(t) for _, t in parts) / G
    for h, t in parts:
        cur.append((h, t)); curlen += len(t)
        if len(groups) < G - 1 and curlen >= want_each:
            groups.append(cur); cur, curlen = [], 0
    if cur:
        groups.append(cur)
    stored = state.setdefault("condensed", {})
    tg.send(f"📝 Total over budget — condensing {n} chapters into {len(groups)} "
            f"parts (~{target} chars)…")
    out = []
    for gi, g in enumerate(groups):
        key = f"g{gi}"
        if key in stored and len(stored[key]) <= int(per_group * 1.2):
            out.append((g[0][0], stored[key]))
            continue
        src = "\n\n".join(t for _, t in g)
        words = max(80, per_group // 5)
        msgs = [{"role": "system", "content": CONDENSE_SYS},
                {"role": "user", "content":
                 f"Compress the following narration to about {words} words:\n\n{src}"}]
        text, rung, fails = llm.call_chain(
            msgs, max_tokens=8000, min_len=max(200, per_group // 4),
            max_len=per_group, tag=f"condense-{key}",
            notified=state.setdefault("notified_rungs", []))
        stored[key] = text
        state["fail_log"] = fails
        out.append((g[0][0], text))
        yield ("condense", key, rung)
    return out


def build_chunks(chapters, limit=None):
    chunks = []
    for ci, ch in enumerate(chapters):
        for c in extract.chunks_from_lines(ch["lines"], limit or config.CHUNK_CHARS):
            chunks.append({"id": f"c{len(chunks)}", "ch": ci, "text": c})
    return chunks


def _ensure_chunks(state, chapters):
    # In-flight books keep their original chunk size (else checkpoints wipe).
    cchars = state.get("chunk_chars", 10000)
    chunks = build_chunks(chapters, cchars)
    if not state["chunks"] or len(state["chunks"]) != len(chunks):
        state["chunks"] = [{"id": c["id"], "ch": c["ch"]} for c in chunks]
        state["chunks_done"] = {}
    state["chunk_chars"] = cchars
    return chunks


def summarize_map(state, chapters):
    """Map phase only (worker pool). Idempotent: skips finished chunks."""
    chunks = _ensure_chunks(state, chapters)
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


def summarize_reduce(state, chapters, workdir):
    """Reduce phase only (smaller pool) + script assembly. Idempotent.
    Global length budget: per-chapter target derived from total minutes, so the
    assembled script lands near target instead of preserving every fragment."""
    chunks = _ensure_chunks(state, chapters)
    lock = threading.Lock()
    total_target = state.get("minutes", config.DEFAULT_MINUTES) * config.CHARS_PER_MIN
    per_ch = max(300, total_target // max(1, len(chapters)))
    max_ch = max(1500, per_ch * 4)
    words = max(60, per_ch // 5)
    # retroactive: chapters done by unbudgeted runs get redone, not kept
    _done = state.get("chapters_done", {})
    redone = [k for k, v in _done.items() if len(v) > max_ch]
    for k in redone:
        del _done[k]
    if redone:
        print(f"{len(redone)} chapters over budget (>{max_ch} chars) — redoing")

    def do_reduce(job):
        ci, ch = job
        key = f"ch{ci}"
        src = "\n\n".join(state["chunks_done"][c["id"]] for c in chunks if c["ch"] == ci)
        msgs = [{"role": "system", "content": REDUCE_SYS},
                {"role": "user", "content":
                 f"Turn these summaries of the chapter '{ch['title']}' into flowing "
                 f"narration of about {words} words:\n\n{src}"}]
        out, rung, fails = llm.call_chain(
            msgs, max_tokens=config.REDUCE_MAX_TOKENS,
            min_len=max(150, min(400, len(src) // 8)), max_len=max_ch,
            tag=f"reduce-{key}",
            notified=state.setdefault("notified_rungs", []))
        with lock:
            state.setdefault("chapters_done", {})[key] = out
            state["fail_log"] = fails
            rn, rtotal = len(state["chapters_done"]), len(chapters)
        if config.CHUNK_SPAM and fails:
            detail = " → ".join(f"{k}×{v}" for k, v in fails.items()) + f" → {rung} ok"
            tg.send(f"🧩 reduce-{key}: {detail}")
        rtick = (f"📝 Assembling — {rn}/{rtotal} chapters"
                 if rn % 20 == 0 or rn == rtotal else None)
        return (ci, rung, rtick)

    jobs = [(ci, ch) for ci, ch in enumerate(chapters)
            if f"ch{ci}" not in state.get("chapters_done", {})]
    with cf.ThreadPoolExecutor(max_workers=config.REDUCE_WORKERS) as ex:
        for fut in cf.as_completed([ex.submit(do_reduce, j) for j in jobs]):
            ci, rung, rtick = fut.result()
            if rtick:
                tg.send(rtick)
            yield ("reduce", f"ch{ci}", rung)

    narrations = {ci: state["chapters_done"][f"ch{ci}"] for ci in range(len(chapters))}
    target = state.get("minutes", config.DEFAULT_MINUTES) * config.CHARS_PER_MIN

    parts = [(chapters[ci]["title"], narrations[ci]) for ci in range(len(chapters))]
    total = sum(len(t) for _, t in parts)
    if total > target * config.CONDENSE_OVER:
        parts = yield from _condense(state, parts, target)
        total = sum(len(t) for _, t in parts)

    title, author = state["title"], state["author"]
    md = [f"# {title} — {author}".strip(" —"), ""]
    for h, t in parts:
        md += [f"## {h}", "", t.strip(), ""]
    md += ["## The last word", "",
           f"That is the heart of {title}. Take one idea from it and use it this week. "
           f"Small steps, repeated, is how every lasting change is built.", ""]
    script = "\n".join(md)
    (workdir / "script.md").write_text(script, encoding="utf-8")
    state["script_chars"] = len(script)
    print(f"script {len(script)} chars vs target ~{target} ({len(script)/target:.0%})")


def summarize(state, chapters, workdir):
    """Full map + reduce (local runs, back-compat). Workflow uses phases."""
    yield from summarize_map(state, chapters)
    yield from summarize_reduce(state, chapters, workdir)
