"""Part loop: synth section -> accumulate -> finalize+send at <=40 MB -> next.
Each part is delivered the moment it is finalized; sent parts are never remade."""
import re

from . import config, synth, tg


def slugify(title):
    s = re.sub(r"[^a-z0-9]+", "", title.lower()) or "book"
    return s[:24]


def deliver(state, workdir, sections):
    """sections: [(heading, body)]. Drives synth + part finalization to completion.
    Yields progress events; updates state in place (caller checkpoints)."""
    title = state["title"]
    album = title
    done = set(state["synth"]["done"])
    sent = state["synth"]["parts_sent"]
    part_n = len(sent)
    cur, cur_bytes, cur_heads = [], 0, []

    def flush(final=False):
        nonlocal part_n, cur, cur_bytes, cur_heads
        if not cur:
            return
        part_n += 1
        hint = f" (final, {part_n} of {part_n})" if final else ""
        m4b, mins = synth.finalize_part(workdir, cur, cur_heads, title, album, part_n)
        mb = m4b.stat().st_size / 1024 / 1024
        cap = f"🎧 {title}\nPart {part_n}{hint}\nDuration: {mins:.1f} min"
        if not tg.send_document(str(m4b), cap):
            raise RuntimeError(f"Telegram upload failed for part {part_n}")
        sent.append({"n": part_n, "mins": round(mins, 1), "mb": round(mb, 1)})
        # part file stays on disk for the artifact upload; workdir is wiped at book-done
        print(f"part {part_n} sent: {mins:.1f} min, {mb:.1f} MB")
        cur, cur_bytes, cur_heads = [], 0, []

    for idx, (h, b) in enumerate(sections):
        if idx not in done:
            synth.synth_section(workdir, idx, h, b)
            done.add(idx)
            state["synth"]["done"] = sorted(done)
            yield ("synth", idx)
            if (idx + 1) % 25 == 0:
                tg.send(f"🎙️ Synthesizing — {idx + 1}/{len(sections)} sections")
        est = int(len(h + b) * config.EST_BYTES_PER_CHAR)
        if cur and cur_bytes + est > config.PART_MAX_MB * 1024 * 1024:
            flush()
            yield ("part", part_n)
        cur.append(idx); cur_bytes += est; cur_heads.append(h)
    flush(final=True)
    yield ("part", part_n)
