"""Caption looks: styled, timed captions rendered by libass.

Adapted from the open-source Subtle caption styles (MIT, Copyright (c) 2026 Refs,
https://github.com/0xpratzyy/subtle): the type pairings, sizes, positions and
paging rules of five of its styles, re-built as ASS so render.py can burn them
in without another renderer. Fonts are in ../fonts (SIL OFL).

A look is chosen per brand in style.json:

    {"caption_look": {"name": "editorial", "accent": "#FF5A1F", "text": "#F2EEE2", "sans": "Archivo"}}

or per edit in edl.json (`captions.look`, a name), which wins. `accent` and
`text` recolour the look; `sans` swaps its sans-serif for the brand font.

Emphasis: wrap a word in *asterisks* in a caption's text to make it the
keyword. Otherwise the look picks one per page (a number, else the longest
word that is not a filler word).
"""

from __future__ import annotations

import re
from pathlib import Path

# size: font size in % of the frame height (portrait); landscape frames use size * wide.
LOOKS: dict[str, dict] = {
    # A bold sans lowercase line; the keyword ~3x larger in a black italic serif, in the accent colour. Lower left.
    "editorial": {
        "label": "Editorial", "case": "lower", "strip_punct": True,
        "sans": "Inter", "sans_bold": True, "size": 3.8, "wide": 1.6,
        "text": "#FFFFFF", "accent": "#D81B22",
        "key_font": "Playfair Display Black", "key_italic": True, "key_scale": 3.0,
        "anchor": (0.09, 0.70), "align": "left",
        "page": {"max_words": 4, "max_chars": 14, "max_lines": 2, "gap": 0.35},
        "shadow": 0.2,
    },
    # White serif on a dark card; key words in the accent colour, italic. Centre, lower third.
    "cards": {
        "label": "Cards", "case": "sentence", "strip_punct": False,
        "sans": "Instrument Serif", "sans_bold": False, "size": 5.0, "wide": 1.25,
        "text": "#FFFFFF", "accent": "#FF5A4F", "box": "#0E0C0CA8",
        "key_font": "Instrument Serif", "key_italic": True, "key_scale": 1.0,
        "anchor": (0.5, 0.71), "align": "center",
        "page": {"max_words": 9, "max_chars": 26, "max_lines": 2, "gap": 0.45},
        "fade": (170, 130),
    },
    # Heavy sans in a strong colour, every word on screen, the spoken word turns the text colour. Thick outline.
    "newsroom": {
        "label": "Newsroom", "case": "sentence", "strip_punct": False,
        "sans": "Montserrat ExtraBold", "sans_bold": False, "size": 4.3, "wide": 1.2,
        "text": "#FFFFFF", "accent": "#FFE14D", "outline": 0.08,
        "anchor": (0.5, 0.73), "align": "center", "karaoke": True, "emphasis": False,
        "page": {"max_words": 8, "max_chars": 24, "max_lines": 2, "gap": 0.45},
        "fade": (130, 100), "shadow": 0.3,
    },
    # Tiny wide-tracked serif capitals low in the frame, slow blur fade. Arthouse trailer subtitles.
    "cinema": {
        "label": "Cinema", "case": "upper", "strip_punct": False,
        "sans": "Libre Baskerville", "sans_bold": False, "size": 1.9, "wide": 1.3,
        "text": "#EFE9DD", "accent": "#EFE9DD", "tracking": 0.36, "line_height": 2.1,
        "anchor": (0.5, 0.82), "align": "center", "emphasis": False,
        "page": {"max_words": 6, "max_chars": 26, "max_lines": 2, "gap": 0.5},
        "fade": (530, 400), "blur_in": True, "shadow": 0.1,
    },
    # Short-form "pop": heavy caps, 1-3 words, the spoken word jumps in size and turns the accent colour.
    "pop": {
        "label": "Pop", "case": "upper", "strip_punct": True,
        "sans": "Montserrat ExtraBold", "sans_bold": False, "size": 5.6, "wide": 1.15,
        "text": "#FFFFFF", "accent": "#FFE14D", "outline": 0.1,
        "anchor": (0.5, 0.68), "align": "center", "karaoke": True, "pop": 1.18, "emphasis": False,
        "page": {"max_words": 3, "max_chars": 16, "max_lines": 2, "gap": 0.3},
        "fade": (60, 60), "shadow": 0.35,
    },
    # One short line in the accent colour; the key word in a light serif italic. Soft fade with a small drift.
    "accent": {
        "label": "Accent", "case": "lower", "strip_punct": True,
        "sans": "Inter Medium", "sans_bold": False, "size": 5.4, "wide": 1.3,
        "text": "#D8F9AB", "accent": "#D8F9AB", "text_is_accent": True,
        "key_font": "Instrument Serif", "key_italic": True, "key_scale": 1.38,
        "anchor": (0.5, 0.72), "align": "center",
        "page": {"max_words": 3, "max_chars": 22, "max_lines": 1, "gap": 0.3},
        "fade": (130, 100), "drift": 0.004, "shadow": 0.2,
    },
}

# family name -> file in ../fonts, for the Studio preview (libass finds them by family itself)
FONT_FILES = {
    "Inter": "Inter-Bold.ttf", "Inter Medium": "Inter-Medium.ttf", "Montserrat ExtraBold": "Montserrat-ExtraBold.ttf",
    "Playfair Display Black": "PlayfairDisplay-BlackItalic.ttf", "Instrument Serif": "InstrumentSerif-Regular.ttf",
    "Instrument Serif Italic": "InstrumentSerif-Italic.ttf", "Libre Baskerville": "LibreBaskerville-wght.ttf",
}

FILLER = set("""a an and are as at be but by for from had has have he her his i if in is it its it's i'm i've
just like me my of on or our so that the their them then there they this to uh um was we we're we'll were what
when which who will with you your yeah okay oh really very kind sort think would could should know going
want get got make about these those than been being also here thing things lot gonna wanna""".split())


def resolve_look(spec) -> dict | None:
    """spec: a look name, or {"name": ..., "accent": ..., "text": ..., "sans": ...}. Returns the merged look."""
    if not spec:
        return None
    if isinstance(spec, str):
        spec = {"name": spec}
    name = str(spec.get("name", "")).lower()
    if name not in LOOKS:
        print(f"warning: unknown caption look '{name}' (looks: {', '.join(LOOKS)}); using plain captions")
        return None
    look = {**LOOKS[name], "name": name}
    # style.json is edited by people and by Claude; only accept plain colours and font names
    hexok = lambda v: isinstance(v, str) and re.fullmatch(r"#?[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?", v)  # noqa: E731
    spec = {**spec}
    for k in ("accent", "text"):
        if spec.get(k) is not None and not hexok(spec[k]):
            print(f"warning: caption_look.{k} must be a hex colour like #FF5A1F; ignoring it")
            spec.pop(k)
        elif spec.get(k) and not spec[k].startswith("#"):
            spec[k] = "#" + spec[k]
    if spec.get("sans") is not None and not (isinstance(spec["sans"], str) and re.fullmatch(r"[A-Za-z0-9 ]{1,40}", spec["sans"])):
        print("warning: caption_look.sans must be a font family name; ignoring it")
        spec.pop("sans")
    if spec.get("accent"):
        look["accent"] = spec["accent"]
        if look.get("text_is_accent"):
            look["text"] = spec["accent"]
    if spec.get("text") and not look.get("text_is_accent"):
        look["text"] = spec["text"]
    if spec.get("sans") and name in ("editorial", "newsroom", "accent", "pop"):
        look["sans"] = spec["sans"]
        look["sans_bold"] = name in ("editorial", "newsroom", "pop") or bool(spec.get("sans_bold"))
    if spec.get("speakers"):
        # text colour per speaker, in order of first appearance (speaker_0, speaker_1, ...)
        look["speakers"] = [("#" + str(c)).replace("##", "#") for c in spec["speakers"] if hexok(str(c))]
    return look


# ---------------------------------------------------------------- paging

def page_words(words: list[tuple[str, float, float]], look: dict) -> list[list[tuple[str, float, float]]]:
    """Group (text, start, end) words in output time into pages by the look's rules."""
    rule = look["page"]
    pages, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        chars = len(" ".join(x[0] for x in cur))
        nxt_chars = chars + 1 + len(nxt[0]) if nxt else 0
        if (nxt is None or w[0][-1:] in ".!?;:," and len(cur) >= 2
                or (len(w) > 3 and len(nxt) > 3 and w[3] != nxt[3])
                or (nxt[1] - w[2]) >= rule["gap"]
                or len(cur) >= rule["max_words"]
                or nxt_chars > rule["max_chars"] * rule["max_lines"]):
            pages.append(cur)
            cur = []
    return pages


def text_to_words(text: str, a: float, b: float) -> list[tuple[str, float, float]]:
    """A hand-typed caption: spread its words evenly over the cue."""
    toks = text.replace("\\N", " ").split()
    if not toks:
        return []
    step = (b - a) / len(toks)
    return [(t, a + i * step, a + (i + 1) * step) for i, t in enumerate(toks)]


# ---------------------------------------------------------------- ASS

def ass_color(hex_rgba: str, alpha: float | None = None) -> str:
    h = hex_rgba.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    a = 255 - int(h[6:8], 16) if len(h) >= 8 else 0
    if alpha is not None:
        a = int(round(255 * (1 - alpha)))
    return f"&H{a:02X}{b:02X}{g:02X}{r:02X}"


def _case(t: str, look: dict) -> str:
    return t.lower() if look["case"] == "lower" else t.upper() if look["case"] == "upper" else t


def _clean(t: str) -> str:
    return t.replace("{", "(").replace("}", ")").replace("\\", "")


def _pick_emphasis(toks: list[str]) -> int:
    marked = [i for i, t in enumerate(toks) if t.startswith("*") and t.rstrip(",.!?;:").endswith("*")]
    if marked:
        return marked[0]
    nums = [i for i, t in enumerate(toks) if re.search(r"\d", t)]
    if nums:
        return nums[0]
    cands = [(len(re.sub(r"\W", "", t)), -i, i) for i, t in enumerate(toks) if re.sub(r"\W", "", t.lower()) not in FILLER]
    cands = [c for c in cands if c[0] >= 4]
    return max(cands)[2] if cands else -1


def _wrap(toks: list[str], max_chars: int, max_lines: int) -> list[list[int]]:
    lines, cur, n = [], [], 0
    for i, t in enumerate(toks):
        if cur and n + 1 + len(t) > max_chars and len(lines) < max_lines - 1:
            lines.append(cur); cur, n = [], 0
        cur.append(i); n += (1 if n else 0) + len(t)
    if cur:
        lines.append(cur)
    return lines


def _ts(s: float) -> str:
    cs = max(0, int(round(s * 100)))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def build_ass(pages: list[dict], look: dict, W: int, H: int) -> str:
    """pages: [{"a", "b", "words": [(text, start, end)], "pos": [x, y] | None, "scale": float}] in output time."""
    px = look["size"] / 100 * H * (look.get("wide", 1.0) if W > H else 1.0)
    fs = px / 0.87                         # libass sizes by line height, CSS by em box
    shadow = look.get("shadow", 0) * px / 10
    outline = look.get("outline", 0) * px
    text_c, accent_c = ass_color(look["text"]), ass_color(look["accent"])
    shadow_c = ass_color("#000000", 0.5)
    box = look.get("box")
    style = (f"Style: Base,{look['sans']},{fs:.1f},{text_c},{accent_c},"
             f"{ass_color(box) if box else ass_color('#000000', 0.85)},{ass_color(box) if box else shadow_c},"
             f"{-1 if look.get('sans_bold') else 0},0,0,0,100,100,{look.get('tracking', 0) * fs * 0.87:.1f},0,"
             f"{3 if box else 1},{px * 0.38 if box else outline:.1f},{0 if box else shadow:.1f},5,0,0,0,1")
    head = ("[Script Info]\nScriptType: v4.00+\n"
            f"PlayResX: {W}\nPlayResY: {H}\nScaledBorderAndShadow: yes\nWrapStyle: 2\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
            "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n" + style + "\n\n[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    ev: list[str] = []

    def add(a, b, x, y, an, text, extra=""):
        ev.append(f"Dialogue: 0,{_ts(a)},{_ts(b)},Base,,0,0,0,,{{\\an{an}\\pos({x:.0f},{y:.0f}){extra}}}{text}")

    fin, fout = look.get("fade", (60, 60))
    speakers: dict = {}
    spk_colors = [ass_color(c if c.startswith("#") else "#" + c) for c in look.get("speakers") or []]
    for pg in pages:
        a, b, sc = pg["a"], pg["b"], float(pg.get("scale") or 1.0)
        toks = [_clean(_case(w[0], look)) for w in pg["words"]]
        if look.get("strip_punct"):
            toks = [t.rstrip(",;:") for t in toks]
        toks = [t for t in toks if t.strip("*")]
        if not toks:
            continue
        k = _pick_emphasis(toks) if look.get("emphasis", True) else -1
        toks = [t.replace("*", "") for t in toks]
        ax, ay = look["anchor"]
        if pg.get("pos"):
            ax, ay = float(pg["pos"][0]), float(pg["pos"][1])
        X, Y = ax * W, ay * H
        fsz = f"\\fs{fs * sc:.1f}" if abs(sc - 1) > 1e-3 else ""
        spk = next((w[3] for w in pg["words"] if len(w) > 3 and w[3]), None)
        if spk is not None and spk not in speakers:
            speakers[spk] = len(speakers)
        page_c = spk_colors[speakers[spk] % len(spk_colors)] if (spk_colors and spk is not None) else None
        if page_c and not look.get("karaoke"):
            fsz += f"\\c{page_c}"
        fade = f"\\fad({fin},{fout})"
        name = look["name"]

        def key_span(t):
            ks = fs * sc * look.get("key_scale", 1.0)
            return (f"{{\\fn{look['key_font']}\\b0\\i{1 if look.get('key_italic') else 0}\\fs{ks:.1f}\\c{accent_c}}}{t}"
                    f"{{\\fn{look['sans']}\\b{1 if look.get('sans_bold') else 0}\\i0\\fs{fs * sc:.1f}\\c{text_c}}}")

        if name == "editorial":
            pre, post = (toks[:k], toks[k + 1:]) if k >= 0 else (toks, [])
            line = px * sc * 1.2
            kp = px * sc * look["key_scale"]
            rows = ([("s", " ".join(pre), line)] if pre else []) + ([("k", toks[k], kp * 0.78)] if k >= 0 else []) \
                + ([("s", " ".join(post), line)] if post else [])
            centred = bool(pg.get("pos"))
            y = Y - (sum(r[2] for r in rows) / 2 if centred else 0)
            an = 2 if centred else 1
            for kind, text, hgt in rows:
                y += hgt
                if kind == "k":
                    ks = fs * sc * look["key_scale"]
                    add(a, b, X - (0 if centred else kp * 0.04), y + kp * 0.18, an, text,
                        f"\\fn{look['key_font']}\\b0\\i1\\fs{ks:.1f}\\c{accent_c}\\fscx85\\fscy85\\blur3"
                        f"\\t(0,120,\\fscx100\\fscy100\\blur0){fade}")
                else:
                    add(a, b, X, y, an, text, fsz + fade)
            continue

        lines = _wrap(toks, look["page"]["max_chars"], look["page"]["max_lines"])
        lh = px * sc * look.get("line_height", 1.25)
        top = Y - lh * (len(lines) - 1) / 2

        def render_line(idxs, active=None):
            out = []
            for i in idxs:
                t = toks[i]
                if look.get("pop"):
                    base = page_c or text_c
                    if i == active:
                        p = look["pop"] * 100
                        out.append(f"{{\\c{accent_c}\\fscx88\\fscy88\\t(0,90,\\fscx{p:.0f}\\fscy{p:.0f})}}{t}{{\\c{base}\\fscx100\\fscy100}}")
                    else:
                        out.append(f"{{\\c{base}}}{t}")
                elif look.get("karaoke"):
                    out.append(f"{{\\c{text_c if i == active else accent_c}}}{t}")
                elif i == k:
                    out.append(key_span(t))
                else:
                    out.append(t)
            return " ".join(out)

        if look.get("karaoke"):
            times = [w[1] for w in pg["words"]][:len(toks)] + [b]
            for wi in range(len(toks)):
                s0 = a if wi == 0 else max(a, times[wi])
                s1 = b if wi == len(toks) - 1 else min(b, times[wi + 1])
                if s1 - s0 < 0.01:
                    continue
                f = f"\\fad({fin if wi == 0 else 0},{fout if wi == len(toks) - 1 else 0})"
                for li, idxs in enumerate(lines):
                    add(s0, s1, X, top + li * lh, 5, render_line(idxs, wi), fsz + f)
            continue

        extra = fsz + fade
        if look.get("blur_in"):
            extra += f"\\blur6\\t(0,{fin},\\blur0)"
        for li, idxs in enumerate(lines):
            y = top + li * lh
            if look.get("drift"):
                d = look["drift"] * H
                ev.append(f"Dialogue: 0,{_ts(a)},{_ts(b)},Base,,0,0,0,,{{\\an5\\move({X:.0f},{y + d:.0f},{X:.0f},{y:.0f},0,{fin}){extra}}}"
                          + render_line(idxs))
            else:
                add(a, b, X, y, 5, render_line(idxs), extra)
    return head + "\n".join(ev) + "\n"


def write_look_ass(pages: list[dict], look: dict, W: int, H: int, out_path: Path) -> Path:
    out_path.write_text(build_ass(pages, look, W, H))
    return out_path
