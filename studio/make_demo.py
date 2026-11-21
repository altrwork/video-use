#!/usr/bin/env python3
"""Build a fake video-use project to try the studio without footage or API keys.

    python studio/make_demo.py demo_project

Uses macOS `say` per word so word timings are exact, writes Scribe-shaped
transcripts, and a first-draft edl.json like video-use would.
"""
import json, subprocess, sys, tempfile
from pathlib import Path

TAKES = {
    "TAKE_A": ("#1d3557", "So um, ninety percent of what a web agent does is, uh, completely wasted. "
                          "We we fixed this. Video Use edits video the way you talk."),
    "TAKE_B": ("#5a2a27", "Ninety percent of what a web agent does is completely wasted. "
                          "Now you just open the studio, click anything, and tell Claude what to change. "
                          "That's it, um, that's the whole idea."),
}


def dur(p):
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                 "-of", "csv=p=0", str(p)], capture_output=True, text=True).stdout)


def main(root: Path):
    edit = root / "edit"; (edit / "transcripts").mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp())
    for name, (color, text) in TAKES.items():
        tokens = text.split()
        parts, words, t = [], [], 0.6
        sil = tmp / "sil.wav"
        for k, tok in enumerate(tokens):
            wav = tmp / f"{name}_{k}.aiff"
            subprocess.run(["say", "-r", "185", "-o", str(wav), tok.strip(",.")], check=True)
            d = dur(wav)
            words.append({"text": tok, "start": round(t + 0.04, 3), "end": round(t + d - 0.06, 3),
                          "type": "word", "speaker_id": "speaker_0"})
            parts.append(wav); t += d
            gap = 0.55 if tok.endswith(".") else 0.22 if tok.endswith(",") else 0.04
            gp = tmp / f"{name}_{k}_g.wav"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono",
                            "-t", str(gap), str(gp)], check=True)
            parts.append(gp); t += gap
        lst = tmp / f"{name}.txt"
        lst.write_text("file '" + str(tmp / "lead.wav") + "'\n" + "".join(f"file '{p}'\n" for p in parts))
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono",
                        "-t", "0.6", str(tmp / "lead.wav")], check=True)
        audio = tmp / f"{name}.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                        "-ar", "48000", "-ac", "1", str(audio)], check=True)
        total = dur(audio) + 0.5
        vf = (f"drawtext=text='{name}':x=60:y=60:fontsize=64:fontcolor=white,"
              "drawtext=text='src %{pts\\:hms}':x=60:y=140:fontsize=40:fontcolor=white@0.8,"
              "drawbox=x=iw*0.62:y=ih*0.25:w=iw*0.25:h=ih*0.5:color=white@0.15:t=fill")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=1280x720:r=30:d={total}",
                        "-i", str(audio), "-vf", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                        "-shortest", str(root / f"{name}.mp4")], check=True)
        # Scribe-shaped transcript: words + spacing
        out = []
        for i, w in enumerate(words):
            out.append(w)
            if i + 1 < len(words):
                out.append({"text": " ", "start": w["end"], "end": words[i + 1]["start"], "type": "spacing"})
        (edit / "transcripts" / f"{name}.json").write_text(json.dumps({"text": text, "words": out}, indent=2))
        print(f"{name}: {total:.1f}s, {len(words)} words")

    A = json.loads((edit / "transcripts/TAKE_A.json").read_text())["words"]
    B = json.loads((edit / "transcripts/TAKE_B.json").read_text())["words"]
    A = [w for w in A if w["type"] == "word"]; B = [w for w in B if w["type"] == "word"]
    # deliberately imperfect first draft: hook from the take with fillers, then take B's body
    edl = {
        "version": 1,
        "sources": {"TAKE_A": str((root / "TAKE_A.mp4").resolve()), "TAKE_B": str((root / "TAKE_B.mp4").resolve())},
        "ranges": [
            {"source": "TAKE_A", "start": round(A[0]["start"] - 0.05, 3), "end": round(A[13]["end"] + 0.08, 3),
             "beat": "HOOK", "quote": "So um, ninety percent ... completely wasted.", "reason": "first draft"},
            {"source": "TAKE_A", "start": round(A[14]["start"] - 0.05, 3), "end": round(A[23]["end"] + 0.08, 3),
             "beat": "SOLUTION", "quote": "We we fixed this. Video Use edits video the way you talk.", "reason": "first draft"},
            {"source": "TAKE_B", "start": round(B[10]["start"] - 0.05, 3), "end": round(B[-1]["end"] + 0.08, 3),
             "beat": "HOW", "quote": "Now you just open the studio ...", "reason": "first draft"},
        ],
        "grade": "none",
        "overlays": [],
        "subtitles": "master.srt",
    }
    edl["total_duration_s"] = round(sum(r["end"] - r["start"] for r in edl["ranges"]), 3)
    (edit / "edl.json").write_text(json.dumps(edl, indent=2))
    print(f"edl.json → {edit/'edl.json'}")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "demo_project").resolve())
