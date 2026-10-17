"""Epub extraction: chapters as text with inline [FIGURE] markers. Stdlib + bs4/lxml/PIL."""
import io
import re
import zipfile
import xml.etree.ElementTree as ET
import warnings
from bs4 import BeautifulSoup
from bs4 import XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
from PIL import Image

from . import config

BLOCK = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "figcaption",
         "dt", "dd", "pre"}
DECORATIVE_NAMES = ("cover", "title", "logo", "divider", "spacer", "ornament",
                    "border", "bullet", "background", "arrow")


def _opf_path(z):
    try:
        root = ET.fromstring(z.read("META-INF/container.xml"))
        for el in root.iter():
            if el.tag.endswith("rootfile"):
                return el.attrib.get("full-path", "")
    except Exception:
        pass
    for n in z.namelist():
        if n.endswith(".opf"):
            return n
    return ""


def _parse_opf(z, opf_path):
    base = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""
    ns = {"o": "http://www.idpf.org/2007/opf", "d": "http://purl.org/dc/elements/1.1/"}
    root = ET.fromstring(z.read(opf_path))
    meta = {}
    for t in ("title", "creator"):
        el = root.find(f"o:metadata/d:{t}", ns)
        if el is not None and el.text:
            meta[t] = el.text.strip()
    manifest = {}
    for item in root.findall("o:manifest/o:item", ns):
        manifest[item.attrib["id"]] = (base + item.attrib["href"],
                                       item.attrib.get("media-type", ""))
    spine = [i.attrib["idref"] for i in root.findall("o:spine/o:itemref", ns)]
    # fallback when namespaces differ
    if not spine:
        for item in root.iter():
            if item.tag.endswith("itemref") and "idref" in item.attrib:
                spine.append(item.attrib["idref"])
    if not manifest:
        for item in root.iter():
            if item.tag.endswith("item") and "href" in item.attrib:
                manifest[item.attrib.get("id", item.attrib["href"])] = (
                    base + item.attrib["href"], item.attrib.get("media-type", ""))
    return meta, manifest, spine


def _merge_fragments(chapters):
    """Fold sub-threshold fragments into the running group (page-split epubs
    emit hundreds of one-paragraph 'chapters'). Folded titles are kept as ###
    subheadings; figure markers and images ride along untouched. All text is
    still read and summarized — regrouped, never dropped."""
    under = config.MERGE_UNDER_CHARS
    def size(ch):
        return sum(len(ln) for ln in ch["lines"])
    merged = []
    for ch in chapters:
        g = merged[-1] if merged else None
        if g is not None and (size(g) < under or size(ch) < under) \
                and size(g) < config.MERGE_MAX_CHARS:
            if ch["title"] and ch["title"] != g["title"]:
                g["lines"].append("### " + ch["title"])
            g["lines"].extend(ch["lines"])
            g["images"].extend(ch["images"])
        else:
            merged.append({"title": ch["title"], "lines": list(ch["lines"]),
                           "images": list(ch["images"])})
    return merged


def _clean_line(s):
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"https?://\S+", "", s).strip()  # bare URLs carry no audio value
    if re.fullmatch(r"(page\s+\d+|\d+|[ivxlc]+\.?)", s, re.I):
        return ""  # page-number lines
    return s


def _is_decorative(name, w, h, referenced):
    if (w and w < 120) or (h and h < 40):
        return True
    low = name.lower()
    if any(k in low for k in DECORATIVE_NAMES) and not referenced:
        return True
    return False


def extract(epub_path, img_dir):
    """Returns (meta, chapters). chapters: [{title, lines, images:[{n,bytes,ext,caption,context}]}."""
    img_dir.mkdir(parents=True, exist_ok=True)
    z = zipfile.ZipFile(epub_path)
    opf = _opf_path(z)
    meta, manifest, spine = _parse_opf(z, opf)
    chapters, fig_n = [], 0
    for sid in spine:
        href, mt = manifest.get(sid, ("", ""))
        if not href or "image" in mt:
            continue
        try:
            raw = z.read(href)
        except KeyError:
            continue
        soup = BeautifulSoup(raw, "lxml")
        for dead in soup(["script", "style", "nav"]):
            dead.decompose()
        for el in soup.find_all(attrs={"epub:type": re.compile(r"toc|page-list|landmarks")}):
            el.decompose()
        body = soup.body or soup
        # top-level blocks only (skip blocks nested in other blocks -> no doubles)
        blocks = [e for e in body.find_all(list(BLOCK))
                  if not any(p.name in BLOCK for p in e.parents)]
        lines, images, title = [], [], ""
        for b in blocks:
            if b.name in ("h1", "h2", "h3") and not title:
                t = _clean_line(b.get_text(" "))
                if t:
                    title = t
            if b.name in ("h1", "h2"):
                t = _clean_line(b.get_text(" "))
                if t:
                    lines.append("## " + t)
                continue
            if b.name == "h3":
                t = _clean_line(b.get_text(" "))
                if t:
                    lines.append("### " + t)
                continue
            t = _clean_line(b.get_text(" "))
            if t:
                lines.append(t)
        # images in document order, paired with caption/context
        for img in body.find_all("img"):
            src = img.get("src", "")
            iname = src.split("/")[-1].split("?")[0] or f"img{fig_n}"
            ibytes, ext = b"", iname.rsplit(".", 1)[-1].lower() if "." in iname else "jpg"
            for f in z.namelist():
                if f.endswith(src) or f.endswith(iname):
                    try:
                        ibytes = z.read(f)
                    except KeyError:
                        pass
                    break
            caption = ""
            fig = img.find_parent("figure")
            if fig and fig.find("figcaption"):
                caption = _clean_line(fig.find("figcaption").get_text(" "))
            if not caption:
                sib = img.find_next_sibling(class_=re.compile(r"caption", re.I))
                if sib:
                    caption = _clean_line(sib.get_text(" "))
            if not caption and img.get("alt"):
                caption = _clean_line(img["alt"])
            # context: nearest non-empty neighbouring block texts
            prev_txt, next_txt = "", ""
            prev_el = img.find_previous(list(BLOCK - {"figcaption"}))
            next_el = img.find_next(list(BLOCK - {"figcaption"}))
            if prev_el:
                prev_txt = _clean_line(prev_el.get_text(" "))[:300]
            if next_el:
                next_txt = _clean_line(next_el.get_text(" "))[:300]
            context = " ".join(x for x in (prev_txt, next_txt) if x)
            referenced = bool(caption or context)
            w = h = 0
            if ibytes:
                try:
                    with Image.open(io.BytesIO(ibytes)) as im:
                        w, h = im.size
                except Exception:
                    pass
            if _is_decorative(iname, w, h, referenced) or not ibytes:
                continue
            fig_n += 1
            (img_dir / f"fig{fig_n}.{ext}").write_bytes(ibytes)
            images.append({"n": fig_n, "ext": ext, "caption": caption, "context": context})
            marker = f"[FIGURE {fig_n}" + (f": {caption}" if caption else "")
            marker += (f" | {context}" if context else "") + "]"
            # insert marker after the line matching prev context, else append
            placed = False
            if prev_txt:
                for i, ln in enumerate(lines):
                    if prev_txt[:60] in ln:
                        lines.insert(i + 1, marker)
                        placed = True
                        break
            if not placed:
                lines.append(marker)
        lines = [ln for ln in lines if ln]
        if not lines:
            continue
        if not title:
            # Filename fallback is always junk on page-split epubs ("Page 0001"
            # — narrated aloud by TTS). A real number is honest: "Chapter 3".
            title = f"Chapter {len(chapters) + 1}"
        chapters.append({"title": title, "lines": lines, "images": images})
    z.close()
    merged = _merge_fragments(chapters)
    print(f"extract: {len(chapters)} spine items -> {len(merged)} chapters")
    return meta, merged


def chunks_from_lines(lines, limit):
    """Split lines into <=limit-char chunks; never cut adjacent to a FIGURE marker."""
    chunks, cur, curlen = [], [], 0
    for ln in lines:
        ln_len = len(ln) + 1
        if curlen + ln_len > limit and cur:
            # glued cut: extend past marker + up to 2 following lines
            chunks.append("\n".join(cur)); cur, curlen = [], 0
        cur.append(ln); curlen += ln_len
    # fix cuts that landed adjacent to a marker: merge such boundary chunks
    fixed = []
    i = 0
    while i < len(chunks):
        c = chunks[i]
        clines = c.split("\n")
        if (i + 1 < len(chunks) and
                (clines[-1].startswith("[FIGURE") or chunks[i + 1].split("\n")[0].startswith("[FIGURE"))):
            chunks[i + 1] = c + "\n" + chunks[i + 1]
            i += 1
            continue
        fixed.append(c); i += 1
    if cur:
        tail = "\n".join(cur)
        if fixed and (fixed[-1].split("\n")[-1].startswith("[FIGURE") or tail.split("\n")[0].startswith("[FIGURE")):
            fixed[-1] = fixed[-1] + "\n" + tail
        else:
            fixed.append(tail)
    return [c for c in fixed if c.strip()]
