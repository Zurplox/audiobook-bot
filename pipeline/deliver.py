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

    def send_part(m4b, mins):
        nonlocal part_n
        mb = m4b.stat().st_size / 1024 / 1024
        if mb > config.TG_MAX_UPLOAD_MB:
            # Never text-fallback a part into a false "done" — stuck instead.
            raise RuntimeError(f"part {mb:.0f} MB over Telegram cap — refusing false delivery")
        part_n += 1
        cap = f"🎧 {title}\nPart {part_n}\nDuration: {mins:.1f} min"
        if not tg.send_document(str(m4b), cap):
            raise RuntimeError(f"Telegram upload failed for part {part_n}")
        sent.append({"n": part_n, "mins": round(mins, 1), "mb": round(mb, 1)})
        print(f"part {part_n} sent: {mins:.1f} min, {mb:.1f} MB")

    def finalize_checked(idxs, heads, tag, depth=0):
        """Finalize; bisect at section bounds while over PART_MAX_MB, so a bad
        size estimate alone can never strand an undeliverable part."""
        m4b, mins = synth.finalize_part(workdir, idxs, heads, title, album, tag)
        mb = m4b.stat().st_size / 1024 / 1024
        if mb <= config.PART_MAX_MB or len(idxs) <= 1 or depth >= 4:
            return [(m4b, mins)]
        print(f"part oversize ({mb:.0f} MB) — bisecting {len(idxs)} sections")
        mid = len(idxs) // 2
        left = finalize_checked(idxs[:mid], heads[:mid], f"{tag}a", depth + 1)
        right = finalize_checked(idxs[mid:], heads[mid:], f"{tag}b", depth + 1)
        m4b.unlink(missing_ok=True)
        return left + right

    def flush():
        nonlocal cur, cur_bytes, cur_heads
        if not cur:
            return
        for m4b, mins in finalize_checked(cur, cur_heads, f"s{cur[0]}-{cur[-1]}"):
            send_part(m4b, mins)
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
    flush()
    yield ("part", part_n)
