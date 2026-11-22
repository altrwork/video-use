"""Render a video from an EDL.

Implements the HEURISTICS render pipeline in the correct order:

  1. Per-segment extract with color grade + 30ms audio fades baked in
  2. Lossless -c copy concat into base.mp4
  3. If overlays or subtitles: single filter graph that overlays animations
     (with PTS shift so frame 0 lands at the overlay window start)
     and applies `subtitles` filter LAST → final.mp4

Optionally builds a master SRT from the per-source transcripts + EDL
output-timeline offsets, applies the proven force_style (2-word
UPPERCASE chunks, Helvetica 18 Bold, MarginV=35).

Usage:
    python helpers/render.py <edl.json> -o final.mp4
    python helpers/render.py <edl.json> -o preview.mp4 --preview
    python helpers/render.py <edl.json> -o final.mp4 --build-subtitles
    python helpers/render.py <edl.json> -o final.mp4 --no-subtitles
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from caption_looks import page_words, resolve_look, text_to_words, write_look_ass  # noqa: E402

try:
    from grade import get_preset, auto_grade_for_clip  # same directory
except Exception:
    def get_preset(name: str) -> str:
        return ""

    def auto_grade_for_clip(video, start=0.0, duration=None, verbose=False):  # type: ignore
        return "eq=contrast=1.03:saturation=0.98", {}


# -------- Subtitle style (bold-overlay, proven at 1920×1080 and 1080×1920) --
#
# MarginV is NOT taste — it is a platform safe-zone rule.
# TikTok / IG Reels / Shorts UI (caption, username, music, right-rail actions)
# covers roughly the bottom ~25–30% of a 1080×1920 frame. Captions placed near
# the bottom edge get clipped or obscured by the UI. libass auto-scales the
# render canvas relative to PlayResY=288, so MarginV=90 lands the caption
# baseline roughly 30% up from the bottom on any aspect — clear of the UI on
# every major vertical-video platform. Do not drop this below ~75 without a
# specific reason.
DEFAULT_SUB_FORCE_STYLE = (
    "FontName=Helvetica,FontSize=20,Bold=1,"
    "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H00000000,"
    "BorderStyle=1,Outline=2,Shadow=0,"
    "Alignment=2,MarginV=48"
)

# ASS force_style fields we know how to assemble from a structured style.json.
# Any field omitted from style.json falls back to the DEFAULT_SUB_FORCE_STYLE value.
_STYLE_FIELD_DEFAULTS = {
    "FontName": "Helvetica",
    "FontSize": "20",
    "Bold": "1",
    "PrimaryColour": "&H00FFFFFF",
    "OutlineColour": "&H00000000",
    "BackColour": "&H00000000",
    "BorderStyle": "1",
    "Outline": "2",
    "Shadow": "0",
    "Alignment": "2",
    "MarginV": "48",
}


def load_subtitle_style(edit_dir: Path) -> str:
    """Look for style.json and return the ASS force_style string to use.
    Falls back to DEFAULT_SUB_FORCE_STYLE if none is found.

    Checked in order (most specific wins):
      1. <edit_dir>/style.json                    — this session/render only
      2. <edit_dir>/../style.json                  — this dated shoot (videos_dir)
      3. <edit_dir>/../../style.json                — client root, reused across shoots

    style.json can either provide a raw override string:
        {"subtitle_force_style": "FontName=Georgia,FontSize=24,..."}
    or structured fields (missing fields fall back to the default):
        {"subtitle": {"FontName": "Georgia", "PrimaryColour": "&H00E6F1FF"}}
    """
    candidates = [
        edit_dir / "style.json",
        edit_dir.parent / "style.json",
        edit_dir.parent.parent / "style.json",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("subtitle_force_style"):
            print(f"  style: {path} (raw force_style)")
            return data["subtitle_force_style"]
        fields = data.get("subtitle")
        if fields:
            merged = {**_STYLE_FIELD_DEFAULTS, **fields}
            print(f"  style: {path} ({', '.join(fields.keys())} overridden)")
            return ",".join(f"{k}={v}" for k, v in merged.items())
    return DEFAULT_SUB_FORCE_STYLE

def load_caption_look(edl: dict, edit_dir: Path) -> dict | None:
    """The caption look for this render: edl.captions.look (a name) wins over the brand's
    style.json "caption_look" (name plus colour/font overrides), most specific style.json first."""
    brand = None
    for path in (edit_dir / "style.json", edit_dir.parent / "style.json", edit_dir.parent.parent / "style.json"):
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("caption_look"):
            brand = data["caption_look"]
            break
    override = (edl.get("captions") or {}).get("look")
    if override == "none":
        return None
    if override:
        spec = dict(brand) if isinstance(brand, dict) else {}
        spec["name"] = override
        return resolve_look(spec)
    return resolve_look(brand)


def fonts_dir(edit_dir: Path) -> Path:
    """One folder libass can read: the repo's caption fonts plus the client's brand/fonts."""
    out = edit_dir / "_fonts"
    out.mkdir(exist_ok=True)
    for src_dir in (Path(__file__).resolve().parent.parent / "fonts", edit_dir.parent.parent / "brand" / "fonts",
                    edit_dir.parent / "brand" / "fonts"):
        if not src_dir.is_dir():
            continue
        for f in src_dir.iterdir():
            if f.suffix.lower() in (".ttf", ".otf") and not (out / f.name).exists():
                try:
                    (out / f.name).symlink_to(f.resolve())
                except OSError:
                    pass
    return out


# -------- Helpers ------------------------------------------------------------


def run(cmd: list[str], quiet: bool = False) -> None:
    if not quiet:
        print(f"  $ {' '.join(str(c) for c in cmd[:6])}{' …' if len(cmd) > 6 else ''}")
    subprocess.run(cmd, check=True)


# -------- Colour grading only on request ---------------------------------------
#
# The EDL's `grade` field doubles as the place reframes live (a 9:16 crop is a
# `crop=…,scale=…` chain), so it is split: geometry filters always apply, look
# filters (anything that changes colour, contrast, sharpness or noise) apply only
# when the EDL records that the user asked for a look, in `grade_request`.
GEOMETRY_FILTERS = {"crop", "scale", "pad", "transpose", "hflip", "vflip", "rotate", "setsar", "setdar"}


def split_filters(chain: str) -> list[str]:
    """Split an ffmpeg filter chain on top-level commas (not inside quotes, brackets or after a backslash)."""
    out, cur, quote, depth, i = [], [], False, 0, 0
    while i < len(chain):
        c = chain[i]
        if c == "\\" and i + 1 < len(chain):
            cur.append(chain[i:i + 2]); i += 2; continue
        if c == "'":
            quote = not quote
        elif not quote and c == "[":
            depth += 1
        elif not quote and c == "]":
            depth -= 1
        if c == "," and not quote and depth == 0:
            out.append("".join(cur)); cur = []
        else:
            cur.append(c)
        i += 1
    out.append("".join(cur))
    return [f.strip() for f in out if f.strip()]


def filter_name(f: str) -> str:
    return re.split(r"[=@\s]", re.sub(r"^\[[^\]]*\]", "", f).strip(), maxsplit=1)[0]


def gate_grade(edl: dict) -> tuple[str | None, list[str]]:
    """Returns (grade field to use, look filters that were dropped). Without
    `grade_request` only geometry survives and "auto"/presets are ignored."""
    grade = edl.get("grade")
    if not grade or str(edl.get("grade_request") or "").strip():
        return grade, []
    if grade in ("none",):
        return None, []
    if grade == "auto" or re.fullmatch(r"[a-zA-Z0-9_\-]+", str(grade)):
        return None, [str(grade)]
    keep, dropped = [], []
    for f in split_filters(str(grade)):
        (keep if filter_name(f) in GEOMETRY_FILTERS else dropped).append(f)
    return (",".join(keep) or None), dropped


def resolve_grade_filter(grade_field: str | None) -> str:
    """The EDL's 'grade' field can be a preset name, a raw ffmpeg filter, or 'auto'.

    Returns the filter string to embed into the per-segment -vf chain.
    For 'auto', returns the sentinel "__AUTO__" which is resolved per-segment.
    """
    if not grade_field:
        return ""
    if grade_field == "auto":
        return "__AUTO__"
    # Preset names are short identifiers, filter strings contain '=' or ','.
    if re.fullmatch(r"[a-zA-Z0-9_\-]+", grade_field):
        try:
            return get_preset(grade_field)
        except KeyError:
            print(f"warning: unknown preset '{grade_field}', using as raw filter")
            return grade_field
    return grade_field


def resolve_path(maybe_path: str, base: Path) -> Path:
    """Resolve a path that may be absolute or relative to `base`."""
    p = Path(maybe_path)
    if p.is_absolute():
        return p
    return (base / p).resolve()


def resolve_subtitles_path(maybe_path: str, edit_dir: Path) -> Path:
    """Resolve the EDL's subtitles path: relative to the EDL's directory, else the
    current directory (agents often write "edit/master.srt"). A missing file is an
    error: rendering on without it silently ships a video with no captions."""
    candidates = [resolve_path(maybe_path, edit_dir)]
    if not Path(maybe_path).is_absolute():
        candidates.append(Path(maybe_path).resolve())
    for c in candidates:
        if c.exists():
            return c
    tried = ", ".join(str(c) for c in candidates)
    sys.exit(f"subtitles file in EDL not found (tried {tried}). Fix the path or pass --no-subtitles.")


# -------- HDR → SDR tone mapping (HLG / PQ sources) --------------------------
#
# iPhone defaults to HLG HDR in Rec.2020 (and many mirrorless cameras ship PQ).
# If the source is HDR and we only downconvert bit depth (yuv420p10le → yuv420p)
# without tone-mapping, the output is 8-bit but still carries HLG/PQ transfer
# metadata. Players that honor the metadata (screen recorders, most social
# upload re-encodes) interpret 8-bit values in an HDR container and the result
# looks oversaturated / blown out. QuickTime on macOS can hide this locally —
# screen recording and uploaded renders cannot.
#
# Fix: detect HDR via color_transfer and prepend a zscale+tonemap chain to the
# vf graph so the output is clean Rec.709 SDR.

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}  # PQ (HDR10) and HLG

TONEMAP_CHAIN = (
    "zscale=t=linear:npl=100,"
    "format=gbrpf32le,"
    "zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,"
    "zscale=t=bt709:m=bt709:r=tv,"
    "format=yuv420p"
)


def is_hdr_source(video: Path) -> bool:
    """Return True if the source uses a PQ or HLG transfer function."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=color_transfer",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip() in HDR_TRANSFERS
    except subprocess.CalledProcessError:
        return False


def probe_color(video: Path) -> dict:
    """pix_fmt and colour tags of the first video stream ({} if unknown)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=pix_fmt,color_range,color_space,color_transfer,color_primaries,width,height",
             "-of", "json", str(video)],
            capture_output=True, text=True, check=True,
        )
        return (json.loads(out.stdout).get("streams") or [{}])[0]
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError):
        return {}


def bit_depth(pix_fmt: str | None) -> int:
    m = re.search(r"p(\d{2})(le|be)?$", pix_fmt or "")
    return int(m.group(1)) if m else 8


def color_tag_args(c: dict) -> list[str]:
    """Write the colour tags explicitly so players don't have to guess."""
    args = []
    for opt, key in (("-color_primaries", "color_primaries"), ("-color_trc", "color_transfer"),
                     ("-colorspace", "color_space"), ("-color_range", "color_range")):
        v = c.get(key)
        if v and v != "unknown":
            args += [opt, v]
    return args


SDR709 = {"color_primaries": "bt709", "color_transfer": "bt709", "color_space": "bt709", "color_range": "tv"}


def video_profile(sources: list[Path], quality: str) -> dict:
    """One encoder setup for the whole render (the -c copy concat needs every
    segment encoded the same way).

    quality: "draft" | "preview" | "final" | "source".
    "source" keeps the source resolution, keeps HDR / 10-bit when every source has
    it, and encodes at a visually lossless setting.
    """
    first = probe_color(sources[0]) if sources else {}
    if quality == "source":
        colors = [probe_color(s) for s in sources]
        hdr = bool(colors) and all(c.get("color_transfer") in HDR_TRANSFERS for c in colors)
        deep = bool(colors) and all(bit_depth(c.get("pix_fmt")) >= 10 for c in colors)
        if hdr or deep:
            return {"codec": ["-c:v", "libx265", "-preset", "slow", "-crf", "14", "-tag:v", "hvc1",
                              "-x265-params", "log-level=error"],
                    "pix_fmt": "yuv420p10le", "tonemap": False, "scale": False,
                    "tags": first, "audio": "320k"}
        return {"codec": ["-c:v", "libx264", "-preset", "slow", "-crf", "12"], "pix_fmt": "yuv420p",
                "tonemap": True, "scale": False, "tags": None, "audio": "320k"}
    preset, crf = {"draft": ("ultrafast", "28"), "preview": ("medium", "22")}.get(quality, ("fast", "20"))
    return {"codec": ["-c:v", "libx264", "-preset", preset, "-crf", crf], "pix_fmt": "yuv420p",
            "tonemap": True, "scale": True, "tags": None, "audio": "192k"}


def is_portrait_source(video: Path) -> bool:
    """Return True if the displayed video is portrait, including rotation."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries",
             "stream=width,height:stream_side_data=rotation",
             "-of", "json", str(video)],
            capture_output=True, text=True, check=True,
        )
        streams = json.loads(out.stdout).get("streams") or []
        if not streams:
            return False
        stream = streams[0]
        w, h = int(stream["width"]), int(stream["height"])

        # ffmpeg autorotates display-matrix side data before applying filters.
        # Swap coded dimensions for quarter-turns so the scale axis is selected
        # from the dimensions the filter actually sees. A plain metadata tag is
        # intentionally ignored because it does not guarantee autorotation.
        rotation = 0
        for side_data in stream.get("side_data_list") or []:
            if side_data.get("rotation") is not None:
                rotation = side_data["rotation"]
                break
        if int(round(float(rotation))) % 360 in (90, 270):
            w, h = h, w
        return h > w
    except (
        subprocess.CalledProcessError,
        json.JSONDecodeError,
        OSError,
        OverflowError,
        KeyError,
        TypeError,
        ValueError,
    ):
        return False


def parse_fps(value: str) -> str:
    """Validate and canonicalize an ffmpeg frame rate."""
    text = value.strip()
    if len(text) > 32 or not re.fullmatch(
        r"(?:[0-9]+(?:\.[0-9]+)?|[0-9]+/[0-9]+)", text
    ):
        raise argparse.ArgumentTypeError(
            "FPS must be a positive number or rational, e.g. 30 or 30000/1001"
        )
    try:
        rate = Fraction(text)
    except (ValueError, ZeroDivisionError) as exc:
        raise argparse.ArgumentTypeError(
            "FPS must be a positive number or rational, e.g. 30 or 30000/1001"
        ) from exc
    if rate <= 0:
        raise argparse.ArgumentTypeError("FPS must be greater than zero")
    # FFmpeg stores video rates as AVRational (signed 32-bit components).
    # Bounding the reduced fraction keeps every accepted canonical value safe
    # for ffmpeg and makes parse_fps(parse_fps(value)) idempotent.
    max_component = 2_147_483_647
    if rate.numerator > max_component or rate.denominator > max_component:
        raise argparse.ArgumentTypeError("FPS precision or magnitude is too large")
    return f"{rate.numerator}/{rate.denominator}"


def probe_source_fps(video: Path) -> str | None:
    """Return an ffmpeg-ready source rate, preferring the average frame rate.

    ``avg_frame_rate`` represents the observed average and is the better default
    for variable-frame-rate inputs. ``r_frame_rate`` remains a fallback for
    streams where the average is unavailable. Values are normalized to an exact
    rational so rates such as ``30000/1001`` survive without rounding.
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=avg_frame_rate,r_frame_rate",
             "-of", "json", str(video)],
            capture_output=True, text=True, check=True,
        )
        streams = json.loads(out.stdout).get("streams") or []
        if not streams:
            return None
        for field in ("avg_frame_rate", "r_frame_rate"):
            value = streams[0].get(field)
            if value and value != "0/0":
                try:
                    return parse_fps(value)
                except argparse.ArgumentTypeError:
                    continue
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError):
        return None
    return None


# -------- Per-segment extraction (Rule 2 + Rule 3) --------------------------


def extract_segment(
    source: Path,
    seg_start: float,
    duration: float,
    grade_filter: str,
    out_path: Path,
    preview: bool = False,
    draft: bool = False,
    rate: str | None = None,
    mute: bool = False,
    profile: dict | None = None,
) -> None:
    """Extract a cut range as its own MP4 with grade + 30ms audio fades baked in.

    `-ss` before `-i` for fast accurate seeking. Scale to 1080p from 4K.
    Portrait sources (height > width) are scaled by height to preserve orientation.
    `mute=True` keeps the video but silences the audio for this segment only —
    for footage where the visual is wanted but the accompanying dialogue (e.g. an
    off-camera interviewer's question) is not.

    Quality ladder:
      - final (default): 1080p libx264 fast CRF 20
      - preview:         1080p libx264 medium CRF 22 (evaluable for QC)
      - draft:           720p libx264 ultrafast CRF 28 (cut-point check only)
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if profile is None:
        profile = video_profile([source], "draft" if draft else "preview" if preview else "final")
    portrait = is_portrait_source(source)
    if draft:
        scale = "scale=-2:1280:flags=lanczos" if portrait else "scale=1280:-2:flags=lanczos"
    else:
        scale = "scale=-2:1920:flags=lanczos" if portrait else "scale=1920:-2:flags=lanczos"

    vf_parts: list[str] = []
    tonemapped = profile["tonemap"] and is_hdr_source(source)
    if tonemapped:
        vf_parts.append(TONEMAP_CHAIN)
    if profile["scale"]:
        vf_parts.append(scale)
    if grade_filter:
        vf_parts.append(grade_filter)
    vf = ",".join(vf_parts) or "null"
    tags = profile["tags"] if profile["tags"] is not None else (SDR709 if tonemapped else (probe_color(source) or SDR709))

    # 30ms audio fades at both edges (Rule 3) — prevent pops
    fade_out_start = max(0.0, duration - 0.03)
    af = f"afade=t=in:st=0:d=0.03,afade=t=out:st={fade_out_start:.3f}:d=0.03"
    if mute:
        af = f"volume=0,{af}"

    # Frame rate: use the rate the caller resolved once for the whole render
    # (every segment must share it — concat -c copy in Rule 2 requires a uniform
    # frame rate). When called standalone with no rate, preserve this source's
    # own rate; fall back to 24 only if it can't be probed.
    out_rate = rate if rate is not None else (probe_source_fps(source) or "24")

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{seg_start:.3f}",
        "-i", str(source),
        "-t", f"{duration:.3f}",
        "-vf", vf,
        "-af", af,
        *profile["codec"],
        "-pix_fmt", profile["pix_fmt"], "-r", out_rate, *color_tag_args(tags),
        "-c:a", "aac", "-b:a", profile["audio"], "-ar", "48000",
        "-movflags", "+faststart",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def extract_all_segments(
    edl: dict,
    edit_dir: Path,
    preview: bool,
    draft: bool = False,
    fps: str | None = None,
    quality: str | None = None,
) -> list[Path]:
    """Extract every EDL range into edit_dir/clips_graded/seg_NN.mp4.
    Returns the ordered list of segment paths.

    If the EDL `grade` is "auto", analyze each segment range with
    `auto_grade_for_clip` and apply a per-segment subtle correction.
    Otherwise, apply the same preset/raw filter to every segment.
    """
    quality = quality or ("draft" if draft else "preview" if preview else "final")
    grade, dropped = gate_grade(edl)
    if dropped:
        print("colour grade skipped (no grade_request in the EDL — only grade when the user asks): " + ", ".join(dropped))
    resolved = resolve_grade_filter(grade)
    is_auto = resolved == "__AUTO__"
    clips_dir = edit_dir / {"draft": "clips_draft", "preview": "clips_preview", "source": "clips_source"}.get(quality, "clips_graded")
    clips_dir.mkdir(parents=True, exist_ok=True)

    ranges = edl["ranges"]
    sources = edl["sources"]

    # Resolve ONE output frame rate for the entire render and apply it to every
    # segment. The lossless concat (Rule 2, `-c copy`) requires all segments to
    # share a frame rate; probing per-segment would diverge for multi-source
    # EDLs that mix rates (e.g. a 30fps and a 60fps source) and break the concat.
    # Explicit --fps wins; otherwise preserve the first source's rate.
    if fps is not None:
        out_rate = parse_fps(str(fps))
    elif ranges:
        first_src = resolve_path(sources[ranges[0]["source"]], edit_dir)
        out_rate = probe_source_fps(first_src) or "24"
    else:
        out_rate = "24"

    used = list(dict.fromkeys(resolve_path(sources[r["source"]], edit_dir) for r in ranges))
    profile = video_profile(used, quality)
    if quality == "source":
        print(f"source quality: {' '.join(profile['codec'][1:2])} {profile['pix_fmt']}, source resolution"
              + ("" if profile["tonemap"] else ", HDR kept"))
    seg_paths: list[Path] = []
    print(f"extracting {len(ranges)} segment(s) → {clips_dir.name}/  @ {out_rate} fps"
          f"{' (forced)' if fps is not None else ' (from source)'}")
    if is_auto:
        print("  (auto-grade per segment: analyzing each range)")
    for i, r in enumerate(ranges):
        src_name = r["source"]
        src_path = resolve_path(sources[src_name], edit_dir)
        start = float(r["start"])
        end = float(r["end"])
        duration = end - start
        out_path = clips_dir / f"seg_{i:02d}_{src_name}.mp4"

        if is_auto:
            seg_filter, _stats = auto_grade_for_clip(src_path, start=start, duration=duration, verbose=False)
        else:
            seg_filter = resolved

        mute = bool(r.get("mute"))
        note = r.get("beat") or r.get("note") or ""
        print(f"  [{i:02d}] {src_name}  {start:7.2f}-{end:7.2f}  ({duration:5.2f}s)  {note}{'  [MUTED]' if mute else ''}")
        if is_auto:
            print(f"        grade: {seg_filter or '(none)'}")
        extract_segment(src_path, start, duration, seg_filter, out_path, preview=preview, draft=draft, rate=out_rate, mute=mute, profile=profile)
        seg_paths.append(out_path)

    return seg_paths


# -------- Lossless concat ----------------------------------------------------


def concat_segments(segment_paths: list[Path], out_path: Path, edit_dir: Path) -> None:
    """Lossless concat via the concat demuxer. No re-encode."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    concat_list = edit_dir / "_concat.txt"
    concat_list.write_text("".join(f"file '{p.resolve()}'\n" for p in segment_paths))

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",
        "-movflags", "+faststart",
        str(out_path),
    ]
    print(f"concat → {out_path.name}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    concat_list.unlink(missing_ok=True)


# -------- Master SRT (Rule 5) ------------------------------------------------


PUNCT_BREAK = set(".,!?;:")


def _srt_timestamp(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _words_in_range(transcript: dict, t_start: float, t_end: float) -> list[dict]:
    out: list[dict] = []
    for w in transcript.get("words", []):
        if w.get("type") != "word":
            continue
        ws = w.get("start")
        we = w.get("end")
        if ws is None or we is None:
            continue
        if we <= t_start or ws >= t_end:
            continue
        out.append(w)
    return out


CHUNK_WORDS = 2         # target words per cue
CHUNK_MAX_WORDS = 3     # a too-short chunk may grow to this many words
CHUNK_MIN_S = 0.35      # a cue shorter than this reads as a flash
CHUNK_PAUSE_S = 0.3     # a gap this long between words ends the cue


def chunk_words(words: list[dict], max_words: int = CHUNK_WORDS) -> list[list[dict]]:
    """Group transcript words into caption cues.

    A cue closes on trailing punctuation or on a pause before the next word.
    Otherwise it closes at CHUNK_WORDS words, unless it would be on screen for
    less than CHUNK_MIN_S; then it takes up to CHUNK_MAX_WORDS words. So
    "does | is" across a pause stays split, and fast "what a" does not flash.
    """
    target, grow = max_words, max(max_words, CHUNK_MAX_WORDS - CHUNK_WORDS + max_words)
    words = [w for w in words if (w.get("text") or "").strip()]
    chunks: list[list[dict]] = []
    current: list[dict] = []
    for i, w in enumerate(words):
        current.append(w)
        text = w["text"].strip()
        nxt = words[i + 1] if i + 1 < len(words) else None
        gap = (nxt["start"] - w["end"]) if nxt else 0.0
        dur = w["end"] - current[0]["start"]
        if (
            nxt is None
            or text[-1] in PUNCT_BREAK
            or gap >= CHUNK_PAUSE_S
            or len(current) >= grow
            or (len(current) >= target and dur >= CHUNK_MIN_S)
        ):
            chunks.append(current)
            current = []
    return chunks


# ASS script space the subtitles filter uses for SRT input (libass PlayRes).
# Caption positions in the EDL are frame fractions; \pos needs these units.
SRT_PLAY_RES = (384, 288)


def caption_entries_from_cues(edl: dict, edit_dir: Path) -> list[tuple[float, float, str]]:
    """Output-timeline caption entries from edl["captions"]["cues"].

    Cues are anchored in SOURCE time ({source, start, end}) so they follow
    their clip through trims and reorders. A cue spanning a cut is split
    into one piece per kept range. Without "text", a cue shows the
    transcript words inside each piece (UPPERCASE), so cuts stay correct;
    with "text" it shows exactly that. "pos" ([x, y] frame fractions, text
    centre) overrides edl["captions"]["pos"], which overrides the style's
    default bottom placement.
    """
    caps = edl.get("captions") or {}
    cues = [c for c in caps.get("cues") or [] if isinstance(c, dict)]
    default_pos = caps.get("pos")
    transcripts: dict[str, dict] = {}

    def words_for(src: str) -> dict:
        if src not in transcripts:
            tp = edit_dir / "transcripts" / f"{src}.json"
            transcripts[src] = json.loads(tp.read_text()) if tp.exists() else {}
        return transcripts[src]

    entries: list[tuple[float, float, str]] = []
    offset = 0.0
    for r in edl["ranges"]:
        rs, re_ = float(r["start"]), float(r["end"])
        if not r.get("mute"):
            for c in cues:
                if c.get("source") != r["source"]:
                    continue
                a, b = max(rs, float(c["start"])), min(re_, float(c["end"]))
                if b - a < 0.05:
                    continue
                if c.get("text") is not None:
                    text = str(c["text"])
                else:
                    ws = _words_in_range(words_for(r["source"]), a, b)
                    text = re.sub(r"\s+", " ", " ".join((w.get("text") or "").strip() for w in ws)).strip()
                    text = text.rstrip(",;:").upper()
                text = text.replace("{", "(").replace("}", ")").replace("\n", "\\N").strip()
                if not text:
                    continue
                pos = c.get("pos") or default_pos
                scale = float(c.get("scale") or caps.get("scale") or 1.0)
                tags = ""
                if pos:
                    x, y = float(pos[0]) * SRT_PLAY_RES[0], float(pos[1]) * SRT_PLAY_RES[1]
                    tags += f"\\an5\\pos({x:.0f},{y:.0f})"
                if abs(scale - 1.0) > 1e-3:
                    tags += f"\\fscx{scale * 100:.0f}\\fscy{scale * 100:.0f}"
                if tags:
                    text = "{" + tags + "}" + text
                entries.append((a - rs + offset, b - rs + offset, text))
        offset += re_ - rs
    return entries


def look_pages(edl: dict, edit_dir: Path, look: dict) -> list[dict]:
    """Output-time pages for a caption look: hand-edited cues as they are, otherwise
    transcript words paged by the look's rules (never across a cut)."""
    caps = edl.get("captions") or {}
    cues = [c for c in caps.get("cues") or [] if isinstance(c, dict)]
    transcripts: dict[str, dict] = {}

    def words_for(src: str) -> dict:
        if src not in transcripts:
            tp = edit_dir / "transcripts" / f"{src}.json"
            transcripts[src] = json.loads(tp.read_text()) if tp.exists() else {}
        return transcripts[src]

    pages: list[dict] = []
    offset = 0.0
    for r in edl["ranges"]:
        rs, re_ = float(r["start"]), float(r["end"])
        if not r.get("mute"):
            to_out = lambda t: max(rs, min(re_, t)) - rs + offset  # noqa: E731
            if cues:
                for c in cues:
                    if c.get("source") != r["source"]:
                        continue
                    a, b = max(rs, float(c["start"])), min(re_, float(c["end"]))
                    if b - a < 0.05:
                        continue
                    if c.get("text") is not None:
                        words = text_to_words(str(c["text"]), to_out(a), to_out(b))
                    else:
                        words = [((w.get("text") or "").strip(), to_out(w["start"]), to_out(w["end"]), w.get("speaker_id"))
                                 for w in _words_in_range(words_for(r["source"]), a, b)]
                    if words:
                        pages.append({"a": to_out(a), "b": to_out(b), "words": words,
                                      "pos": c.get("pos") or caps.get("pos"), "scale": c.get("scale") or caps.get("scale")})
            else:
                words = [((w.get("text") or "").strip(), to_out(w["start"]), to_out(w["end"]), w.get("speaker_id"))
                         for w in _words_in_range(words_for(r["source"]), rs, re_)]
                words = [w for w in words if w[0]]
                for pg in page_words(words, look):
                    b = pg[-1][2]
                    pages.append({"a": pg[0][1], "b": b, "words": pg, "pos": caps.get("pos"), "scale": caps.get("scale")})
        offset += re_ - rs
    pages.sort(key=lambda p: p["a"])
    # hold each page until the next one starts (short gaps only), so captions don't blink
    for p, n in zip(pages, pages[1:]):
        if 0 < n["a"] - p["b"] < 0.5:
            p["b"] = n["a"]
    return pages


def _ass_timestamp(seconds: float) -> str:
    cs = max(0, int(round(seconds * 100)))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def write_ass(entries: list[tuple[float, float, str]], out_path: Path) -> None:
    """Minimal ASS script in the same 384x288 space libass uses for SRT, so
    force_style (style.json) applies identically and \\pos tags work (libass
    ignores \\pos in SRT input)."""
    head = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 384\nPlayResY: 288\n"
        "ScaledBorderAndShadow: yes\nWrapStyle: 0\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Arial,16,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,2,10,10,10,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = [f"Dialogue: 0,{_ass_timestamp(a)},{_ass_timestamp(b)},Default,,0,0,0,,{t}" for a, b, t in entries]
    out_path.write_text(head + "\n".join(lines) + "\n")


def build_master_srt(edl: dict, edit_dir: Path, out_path: Path, max_words: int = CHUNK_WORDS,
                     look: dict | None = None, dims: tuple[int, int] | None = None) -> Path:
    """Build an output-timeline SRT from per-source transcripts.

    If edl["captions"]["cues"] exists (hand-edited captions, e.g. from the
    Studio), those are used instead of auto-chunking — see
    caption_entries_from_cues.

    - phrase-aware ~max_words-word chunks (see chunk_words)
    - UPPERCASE text
    - Output times computed as word.start - segment_start + segment_offset
    """
    transcripts_dir = edit_dir / "transcripts"

    if look:
        pages = look_pages(edl, edit_dir, look)
        lines = []
        for i, p in enumerate(pages, start=1):
            text = " ".join(w[0].replace("*", "") for w in p["words"])
            lines += [str(i), f"{_srt_timestamp(p['a'])} --> {_srt_timestamp(p['b'])}", text, ""]
        out_path.write_text("\n".join(lines))
        W, H = dims or (1080, 1920)
        ass_path = write_look_ass(pages, look, W, H, out_path.with_suffix(".look.ass"))
        print(f"captions → {ass_path.name} ({len(pages)} pages, look: {look['name']}); plain {out_path.name} alongside")
        return ass_path

    entries: list[tuple[float, float, str]] = []
    seg_offset = 0.0
    cue_mode = bool((edl.get("captions") or {}).get("cues"))
    if cue_mode:
        entries = caption_entries_from_cues(edl, edit_dir)

    for r in ([] if cue_mode else edl["ranges"]):
        src_name = r["source"]
        seg_start = float(r["start"])
        seg_end = float(r["end"])
        seg_duration = seg_end - seg_start

        if r.get("mute"):
            # No audio, no caption — muted ranges keep the visual only.
            seg_offset += seg_duration
            continue

        tr_path = transcripts_dir / f"{src_name}.json"
        if not tr_path.exists():
            print(f"  no transcript for {src_name}, skipping captions for this segment")
            seg_offset += seg_duration
            continue

        transcript = json.loads(tr_path.read_text())
        words_in_seg = _words_in_range(transcript, seg_start, seg_end)

        for chunk in chunk_words(words_in_seg, max_words):
            local_start = max(seg_start, chunk[0].get("start", seg_start))
            local_end = min(seg_end, chunk[-1].get("end", seg_end))
            out_start = max(0.0, local_start - seg_start) + seg_offset
            out_end = max(0.0, local_end - seg_start) + seg_offset
            if out_end <= out_start:
                out_end = out_start + 0.4
            text = " ".join((w.get("text") or "").strip() for w in chunk)
            text = re.sub(r"\s+", " ", text).strip()
            # Strip trailing punctuation for cleaner uppercase look
            text = text.rstrip(",;:")
            text = text.upper()
            entries.append((out_start, out_end, text))

        seg_offset += seg_duration

    # Sort and write as SRT
    entries.sort(key=lambda e: e[0])
    lines: list[str] = []
    for i, (a, b, t) in enumerate(entries, start=1):
        lines.append(str(i))
        lines.append(f"{_srt_timestamp(a)} --> {_srt_timestamp(b)}")
        lines.append(re.sub(r"\{\\[^}]*\}", "", t).replace("\\N", "\n"))  # plain SRT: no ASS tags
        lines.append("")
    out_path.write_text("\n".join(lines))
    print(f"master SRT → {out_path.name} ({len(entries)} cues)")
    if cue_mode:
        # positioned / edited captions need ASS; the SRT above stays as a plain-text sidecar
        ass_path = out_path.with_suffix(".ass")
        write_ass(entries, ass_path)
        print(f"master ASS → {ass_path.name} (edited captions)")
        return ass_path
    return out_path


# -------- Loudness normalization (social-ready audio) -----------------------


# Social-media standard: -14 LUFS integrated, -1 dBTP peak, LRA 11 LU.
# Matches YouTube / Instagram / TikTok / X / LinkedIn normalization targets.
LOUDNORM_I = -14.0
LOUDNORM_TP = -1.0
LOUDNORM_LRA = 11.0


def measure_loudness(video_path: Path) -> dict[str, str] | None:
    """Run ffmpeg loudnorm first pass and parse the JSON measurement.

    Returns a dict with measured_i, measured_tp, measured_lra, measured_thresh,
    target_offset, or None if measurement failed.
    """
    filter_str = (
        f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}:print_format=json"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-i", str(video_path),
        "-af", filter_str,
        "-vn", "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    # loudnorm prints the JSON to stderr at the end of the run
    stderr = proc.stderr

    # Find the JSON block — loudnorm output contains a `{ ... }` block
    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(stderr[start : end + 1])
    except json.JSONDecodeError:
        return None
    needed = {"input_i", "input_tp", "input_lra", "input_thresh", "target_offset"}
    if not needed.issubset(data.keys()):
        return None
    return data


def apply_loudnorm_two_pass(
    input_path: Path,
    output_path: Path,
    preview: bool = False,
) -> bool:
    """Run two-pass loudnorm on input_path, write normalized copy to output_path.

    Returns True on success, False if measurement failed (caller should fall
    back to copying the input unchanged).

    In preview mode, skips the measurement pass and uses a one-pass approximation
    for speed. Final mode always does the proper two-pass.
    """
    if preview:
        # One-pass approximation — faster, slightly less accurate.
        filter_str = f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}"
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-nostats",
            "-i", str(input_path),
            "-c:v", "copy",
            "-af", filter_str,
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart",
            str(output_path),
        ]
        print(f"  loudnorm (1-pass preview) → {output_path.name}")
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return True

    # Full two-pass
    print(f"  loudnorm pass 1: measuring {input_path.name}")
    measurement = measure_loudness(input_path)
    if measurement is None:
        print("  loudnorm measurement failed — falling back to 1-pass")
        return apply_loudnorm_two_pass(input_path, output_path, preview=True)

    print(f"    measured: I={measurement['input_i']} LUFS  "
          f"TP={measurement['input_tp']}  LRA={measurement['input_lra']}")

    filter_str = (
        f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}"
        f":measured_I={measurement['input_i']}"
        f":measured_TP={measurement['input_tp']}"
        f":measured_LRA={measurement['input_lra']}"
        f":measured_thresh={measurement['input_thresh']}"
        f":offset={measurement['target_offset']}"
        f":linear=true"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-i", str(input_path),
        "-c:v", "copy",
        "-af", filter_str,
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(output_path),
    ]
    print(f"  loudnorm pass 2: normalizing → {output_path.name}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return True


# -------- Final compositing (Rule 1 + Rule 4) -------------------------------


def probe_dims(video: Path) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "csv=p=0", str(video)], capture_output=True, text=True, check=True).stdout
    w, h = [int(v) for v in out.strip().split(",")[:2]]
    return w, h


def overlay_xy(ov: dict, base_dims: tuple[int, int]) -> str:
    """x=..:y=..: for overlays placed with fractional x/y (top-left); '' keeps 0,0."""
    if ov.get("x") is None and ov.get("y") is None:
        return ""
    bw, bh = base_dims
    return f"x={round(float(ov.get('x') or 0) * bw)}:y={round(float(ov.get('y') or 0) * bh)}:"


def build_final_composite(
    base_path: Path,
    overlays: list[dict],
    subtitles_path: Path | None,
    out_path: Path,
    edit_dir: Path,
    force_style: str = DEFAULT_SUB_FORCE_STYLE,
    profile: dict | None = None,
) -> None:
    """Final pass: base → overlays (PTS-shifted) → subtitles LAST → out.

    If there are no overlays and no subtitles, just copy base to out.
    """
    has_overlays = bool(overlays)
    base_dims = probe_dims(base_path) if overlays else (0, 0)
    # Unplaced graphics are made for the standard 1920 frame; at source resolution scale them to match.
    native_k = max(base_dims) / 1920 if overlays and max(base_dims) else 1.0
    has_subs = subtitles_path is not None and subtitles_path.exists()

    if not has_overlays and not has_subs:
        # Nothing to do — just rename/copy base to final name
        run(["ffmpeg", "-y", "-i", str(base_path), "-c", "copy", str(out_path)], quiet=True)
        return

    inputs: list[str] = ["-i", str(base_path)]
    for ov in overlays:
        ov_path = resolve_path(ov["file"], edit_dir)
        inputs += ["-i", str(ov_path)]

    filter_parts: list[str] = []
    # PTS-shift every overlay so its frame 0 lands at start_in_output
    for idx, ov in enumerate(overlays, start=1):
        t = float(ov["start_in_output"])
        # optional placement: x/y = top-left, w = width, all as fractions of the output frame
        place = ""
        if ov.get("w") is not None:
            bw, bh = base_dims
            pw = max(2, int(round(float(ov["w"]) * bw / 2)) * 2)
            place = f",scale={pw}:-2"
        elif abs(native_k - 1.0) > 0.01:
            place = f",scale=trunc(iw*{native_k:.4f}/2)*2:-2"
        filter_parts.append(f"[{idx}:v]setpts=PTS-STARTPTS+{t}/TB{place}[a{idx}]")

    # Chain overlays on top of base
    current = "[0:v]"
    for idx, ov in enumerate(overlays, start=1):
        t = float(ov["start_in_output"])
        dur = float(ov["duration"])
        end = t + dur
        next_label = f"[v{idx}]"
        filter_parts.append(
            f"{current}[a{idx}]overlay={overlay_xy(ov, base_dims)}enable='between(t,{t:.3f},{end:.3f})'{next_label}"
        )
        current = next_label

    # Subtitles LAST — Rule 1
    if has_subs:
        subs_abs = str(subtitles_path.resolve()).replace(":", r"\:").replace("'", r"\'")
        fdir = str(fonts_dir(edit_dir)).replace(":", r"\:").replace("'", r"\'")
        style_opt = "" if subtitles_path.name.endswith(".look.ass") else f":force_style='{force_style}'"
        filter_parts.append(
            f"{current}subtitles='{subs_abs}':fontsdir='{fdir}'{style_opt}[outv]"
        )
        out_label = "[outv]"
    else:
        # Rename the last overlay output to [outv] for consistency
        if has_overlays:
            filter_parts.append(f"{current}null[outv]")
            out_label = "[outv]"
        else:
            out_label = "[0:v]"

    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", out_label,
        "-map", "0:a",
        *(profile["codec"] if profile else ["-c:v", "libx264", "-preset", "fast", "-crf", "18"]),
        "-pix_fmt", profile["pix_fmt"] if profile else "yuv420p", *color_tag_args(probe_color(base_path)),
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(out_path),
    ]
    print(f"compositing → {out_path.name}")
    print(f"  overlays: {len(overlays)}, subtitles: {'yes' if has_subs else 'no'}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


# -------- Main ---------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="Render a video from an EDL")
    ap.add_argument("edl", type=Path, help="Path to edl.json")
    ap.add_argument("-o", "--output", type=Path, required=True, help="Output video path")
    ap.add_argument(
        "--preview",
        action="store_true",
        help="Preview mode: 1080p, medium, CRF 22 — evaluable for QC, faster than final.",
    )
    ap.add_argument(
        "--draft",
        action="store_true",
        help="Draft mode: 720p, ultrafast, CRF 28 — cut-point verification only.",
    )
    ap.add_argument(
        "--source-quality",
        action="store_true",
        help="Keep the source's resolution, frame rate and (when every source has it) HDR / 10-bit; "
             "visually lossless encode; no loudness change unless --loudnorm.",
    )
    ap.add_argument(
        "--loudnorm",
        action="store_true",
        help="Normalize loudness even with --source-quality.",
    )
    ap.add_argument(
        "--build-subtitles",
        action="store_true",
        help="Build master.srt from transcripts + EDL offsets before compositing",
    )
    ap.add_argument(
        "--max-caption-words",
        type=int,
        default=2,
        help="Max words per caption chunk when building subtitles (default: 2)",
    )
    ap.add_argument(
        "--no-subtitles",
        action="store_true",
        help="Skip subtitles even if the EDL references one",
    )
    ap.add_argument(
        "--no-loudnorm",
        action="store_true",
        help="Skip audio loudness normalization. Default is on (-14 LUFS, -1 dBTP, LRA 11).",
    )
    ap.add_argument(
        "--fps",
        type=parse_fps,
        default=None,
        help="Output frame rate. Default: preserve the source's frame rate "
             "(falls back to 24 if it can't be probed). Pass e.g. --fps 30 or "
             "--fps 30000/1001 to force.",
    )
    args = ap.parse_args()

    edl_path = args.edl.resolve()
    if not edl_path.exists():
        sys.exit(f"edl not found: {edl_path}")

    edl = json.loads(edl_path.read_text())
    edit_dir = edl_path.parent
    out_path = args.output.resolve()

    quality = "source" if args.source_quality else "draft" if args.draft else "preview" if args.preview else "final"
    no_loudnorm = args.no_loudnorm or (args.source_quality and not args.loudnorm)

    # 1. Extract per-segment (auto-grade per range if EDL grade is "auto")
    segment_paths = extract_all_segments(
        edl, edit_dir, preview=args.preview, draft=args.draft, fps=args.fps, quality=quality
    )
    used = list(dict.fromkeys(resolve_path(edl["sources"][r["source"]], edit_dir) for r in edl["ranges"]))
    profile = video_profile(used, quality)

    # 2. Concat → base
    base_name = {"draft": "base_draft.mp4", "preview": "base_preview.mp4", "source": "base_source.mp4"}.get(quality, "base.mp4")
    base_path = edit_dir / base_name
    concat_segments(segment_paths, base_path, edit_dir)

    # 3. Subtitles: build if requested, resolve final path
    subs_path: Path | None = None
    if not args.no_subtitles:
        if args.build_subtitles or (edl.get("captions") or {}).get("cues"):
            subs_path = edit_dir / "master.srt"
            look = load_caption_look(edl, edit_dir)
            subs_path = build_master_srt(edl, edit_dir, subs_path, max_words=args.max_caption_words,
                                         look=look, dims=probe_dims(base_path) if look else None)
        elif edl.get("subtitles"):
            subs_path = resolve_subtitles_path(edl["subtitles"], edit_dir)

    # 4. Composite (overlays + subtitles LAST) → intermediate (pre-loudnorm) path
    overlays = edl.get("overlays") or []
    force_style = load_subtitle_style(edit_dir)
    if no_loudnorm:
        # Composite directly to final output
        build_final_composite(base_path, overlays, subs_path, out_path, edit_dir, force_style, profile)
    else:
        # Composite to a temp file, then run loudnorm → final output
        tmp_composite = out_path.with_suffix(".prenorm.mp4")
        build_final_composite(base_path, overlays, subs_path, tmp_composite, edit_dir, force_style, profile)
        print("loudness normalization → social-ready (-14 LUFS / -1 dBTP / LRA 11)")
        apply_loudnorm_two_pass(tmp_composite, out_path, preview=args.draft)
        tmp_composite.unlink(missing_ok=True)

    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"\ndone: {out_path} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
