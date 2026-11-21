#!/usr/bin/env python3
"""Video Use Studio — cloud companion.

Runs on the editing computer next to the footage. It publishes one project to
the Cloudflare-hosted Studio (cloud/), keeps edl.json in sync both ways, and
does the work that needs ffmpeg or the raw files: preview copies, renders,
frame grabs, and downloading assets Claude asks for.

Raw footage never leaves this machine; only small preview copies are uploaded.

    python studio/cloud_sync.py <videos_dir>/edit [--cloud https://…workers.dev]

The URL and sync key come from --cloud / VIDEO_USE_CLOUD_URL / VIDEO_USE_CLOUD_KEY
or ~/.config/video-use/cloud.json ({"url": "...", "sync_key": "..."}).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import Project, find_video_use, safe  # noqa: E402

CHUNK = 1024 * 1024
MAX_UPLOAD = 300 * 1024 * 1024
MAX_FETCH = 500 * 1024 * 1024
MEDIA_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".mov", ".webm", ".m4v", ".mp3", ".wav", ".m4a", ".aac", ".ttf", ".otf"}
MIME = {".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime", ".png": "image/png",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif", ".json": "application/json"}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def edl_hash(edl: dict) -> str:
    e = {k: v for k, v in edl.items() if k != "total_duration_s"}
    return hashlib.sha256(json.dumps(e, sort_keys=True).encode()).hexdigest()


class Cloud:
    def __init__(self, url: str, key: str):
        self.url, self.key = url.rstrip("/"), key

    def call(self, path: str, body=None, method=None, raw: bytes | None = None, timeout=60):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.url + path, data=data, method=method or ("POST" if data is not None else "GET"))
        req.add_header("Authorization", "Bearer " + self.key)
        req.add_header("Content-Type", "application/octet-stream" if raw is not None else "application/json")
        req.add_header("User-Agent", "video-use-companion")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{path.split('?')[0]} → {e.code}: {e.read()[:200].decode(errors='replace')}") from None


class Companion:
    def __init__(self, project: Project, cloud: Cloud):
        self.p, self.c = project, cloud
        self.state_file = project.studio_dir / "cloud.json"
        try:
            self.state = json.loads(self.state_file.read_text())
        except Exception:
            self.state = {}
        if not self.state.get("id"):
            base = re.sub(r"[^a-z0-9]+", "-", project.videos_dir.name.lower()).strip("-")[:40] or "project"
            self.state["id"] = f"{base}-{secrets.token_hex(3)}"
        self.id = self.state["id"]
        self.q = "project=" + urllib.parse.quote(self.id)
        self.remote_blobs: dict[str, list] = {}
        self.lock = threading.Lock()
        self.render_proc: subprocess.Popen | None = None
        self.sig = ""

    def save_state(self):
        self.state_file.write_text(json.dumps(self.state))

    # ---- what the cloud needs to show the project ----
    def names(self) -> list[str]:
        return list((self.p.read_edl().get("sources") or {}).keys())

    def docs(self) -> dict:
        names = self.names()
        for n in names:
            if not (self.p.wave_path(n).exists() and self.p.thumbs_meta_path(n).exists()):
                self.p._build_assets(n)
        d = {"media": {n: self.p.probe(n) for n in names}, "assets": {n: self.p.assets(n) for n in names},
             "capstyle": self.p.caption_style(), "bin": self.p.assets_bin(),
             "brand": str(self.p.client_root / "brand.md") if self.p.client_root else None,
             "notes:context": self.p.read_notes("context"), "notes:prefs": self.p.read_notes("prefs"),
             "renders": {k: (int((self.p.edit_dir / f).stat().st_mtime) if (self.p.edit_dir / f).exists() else 0)
                         for k, f in (("draft", "draft.mp4"), ("preview", "preview.mp4"), ("final", "final.mp4"))}}
        for n in names:
            d["transcript:" + n] = self.p.transcript(n)
        return d

    def proxy(self, name: str) -> Path | None:
        src = self.p.source_path(name)
        if not src or not src.exists():
            return None
        out = self.p.cache_dir / f"proxy_{safe(name)}_{int(src.stat().st_mtime)}.mp4"
        if not out.exists():
            log(f"making a preview copy of {name} …")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vf",
                            "scale='if(gt(iw,ih),-2,min(720,iw))':'if(gt(iw,ih),min(720,ih),-2)'", "-c:v", "libx264",
                            "-preset", "veryfast", "-crf", "27", "-g", "30", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k",
                            "-movflags", "+faststart", str(out) + ".tmp.mp4"], check=False)
            tmp = Path(str(out) + ".tmp.mp4")
            if tmp.exists() and tmp.stat().st_size:
                tmp.replace(out)
        return out if out.exists() else None

    def blobs(self) -> list[tuple[str, Path]]:
        out: list[tuple[str, Path]] = []
        for n in self.names():
            pr = self.proxy(n)
            if pr:
                out.append(("media/" + n, pr))
            if self.p.thumbs_path(n).exists():
                out.append(("cache/" + self.p.thumbs_path(n).name, self.p.thumbs_path(n)))
        for ov in self.p.read_edl().get("overlays") or []:
            f = self.p.overlay_proxy(str(ov.get("file", "")))
            if f:
                out.append(("overlay/" + ov["file"], f))
        for a in self.p.assets_bin():
            if a["kind"] == "image" and not a["name"].lower().endswith(".svg"):
                out.append(("file/" + a["path"], Path(a["path"])))
            elif a["kind"] == "video":
                f = self.p.overlay_proxy(a["path"])
                if f:
                    out.append(("overlay/" + a["path"], f))
        for f in ("draft.mp4", "preview.mp4", "final.mp4"):
            if (self.p.edit_dir / f).exists():
                out.append(("file/" + str((self.p.edit_dir / f).relative_to(self.p.videos_dir)), self.p.edit_dir / f))
        return out

    def upload(self, key: str, path: Path) -> bool:
        st = path.stat()
        if self.remote_blobs.get(key) == [st.st_size, int(st.st_mtime)]:
            return False
        if st.st_size > MAX_UPLOAD or st.st_size == 0:
            log(f"skipped {key}: {st.st_size / 1e6:.0f} MB")
            return False
        n = max(1, -(-st.st_size // CHUNK))
        mime = MIME.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with path.open("rb") as fh:
            for i in range(n):
                qs = urllib.parse.urlencode({"project": self.id, "path": key, "idx": i, "n": n, "size": st.st_size,
                                             "type": mime, "mtime": int(st.st_mtime)})
                self.c.call("/sync/blob?" + qs, raw=fh.read(CHUNK), method="PUT", timeout=120)
        self.remote_blobs[key] = [st.st_size, int(st.st_mtime)]
        log(f"uploaded {key} ({st.st_size / 1e6:.1f} MB)")
        return True

    def signature(self) -> str:
        parts = [json.dumps(self.p.assets_bin(), sort_keys=True), self.p.read_notes("context"), self.p.read_notes("prefs"),
                 json.dumps(self.p.caption_style(), sort_keys=True)]
        return hashlib.sha256("\x00".join(parts).encode()).hexdigest()

    def publish(self, edl_push: dict | None = None, keep_cloud_as: str | None = None) -> dict:
        with self.lock:
            body = {"id": self.id, "name": self.label(), "edl": edl_push or self.p.read_edl(), "push": edl_push is not None,
                    "docs": self.docs()}
            if keep_cloud_as:
                body["keep_cloud_as"] = keep_cloud_as
            r = self.c.call("/sync/project", body)
            self.remote_blobs = r.get("blobs", {})
            for key, path in self.blobs():
                try:
                    self.upload(key, path)
                except Exception as e:  # keep going; one bad file must not stop the sync
                    log(f"upload failed for {key}: {e}")
            self.sig = self.signature()
            return r

    def label(self) -> str:
        return (f"{self.p.client_root.name} / " if self.p.client_root else "") + self.p.videos_dir.name

    # ---- start: decide which side's timeline wins ----
    def start(self):
        local = self.p.read_edl()
        lh = edl_hash(local)
        r = self.publish()                       # creates the project if new; never overwrites the cloud timeline
        cloud_edl, rev = r["edl"], r["rev"]
        ch = edl_hash(cloud_edl)
        if ch != lh:
            local_changed, cloud_changed = lh != self.state.get("hash"), rev != self.state.get("rev")
            if cloud_changed and not local_changed:
                self.p.write_edl(cloud_edl)
                lh = ch
                log(f"pulled the cloud timeline (rev {rev})")
            else:
                keep = f"cloud before sync {time.strftime('%Y-%m-%d %H:%M')}" if cloud_changed else None
                rev = self.publish(edl_push=local, keep_cloud_as=keep)["rev"]
                log("pushed the local timeline" + (f"; the cloud version was kept as “{keep}”" if keep else ""))
        self.state.update(hash=lh, rev=rev)
        self.save_state()
        log(f"published “{self.label()}” → {self.c.url}/p/{self.id}/")

    # ---- loops ----
    def watch_local(self):
        last_m = self.p.edl_mtime()
        n = 0
        while True:
            time.sleep(1)
            n += 1
            try:
                m = self.p.edl_mtime()
                if m != last_m:
                    last_m = m
                    time.sleep(0.2)
                    edl = self.p.read_edl()
                    h = edl_hash(edl)
                    if h != self.state.get("hash"):
                        self.state.update(hash=h, rev=self.publish(edl_push=edl)["rev"])
                        self.save_state()
                        log("local timeline change → cloud")
                elif n % 5 == 0 and self.signature() != self.sig:
                    self.publish()
                    log("assets / notes change → cloud")
            except Exception as e:
                log("watch:", e)

    def poll(self):
        after = 0
        while True:
            try:
                r = self.c.call(f"/sync/poll?{self.q}&after={after}", timeout=40)
            except Exception as e:
                log("poll:", e)
                time.sleep(5)
                continue
            for m in r.get("msgs", []):
                after = max(after, m["seq"])
                try:
                    if m["type"] == "edl":
                        if edl_hash(m["edl"]) != self.state.get("hash"):
                            self.state.update(hash=edl_hash(m["edl"]), rev=m["rev"])
                            self.save_state()
                            self.p.write_edl(m["edl"])
                            log(f"cloud timeline change (rev {m['rev']}) → edl.json")
                            threading.Thread(target=self.sync_overlays, daemon=True).start()
                    elif m["type"] == "notes" and m.get("which") in ("context", "prefs"):
                        self.p.write_notes(m["which"], m.get("text", ""))
                        self.sig = self.signature()
                    elif m["type"] == "rpc":
                        threading.Thread(target=self.handle_rpc, args=(m,), daemon=True).start()
                except Exception as e:
                    log("message failed:", e)

    def sync_overlays(self):
        with self.lock:
            for ov in self.p.read_edl().get("overlays") or []:
                f = self.p.overlay_proxy(str(ov.get("file", "")))
                if f:
                    try:
                        if self.upload("overlay/" + ov["file"], f):
                            self.c.call("/sync/event?" + self.q, {"event": "bin", "data": {"overlays": True}})
                    except Exception as e:
                        log("overlay upload:", e)

    # ---- work the cloud asks for ----
    def handle_rpc(self, m: dict):
        try:
            result = getattr(self, "rpc_" + m["method"])(**(m.get("args") or {}))
            self.c.call("/sync/reply?" + self.q, {"id": m["id"], "ok": True, "result": result})
        except Exception as e:
            log(f"{m.get('method')} failed: {e}")
            try:
                self.c.call("/sync/reply?" + self.q, {"id": m["id"], "ok": False, "error": str(e)[:400]})
            except Exception:
                pass

    def event(self, event: str, data: dict):
        try:
            self.c.call("/sync/event?" + self.q, {"event": event, "data": data})
        except Exception as e:
            log("event:", e)

    def rpc_render(self, mode: str = "preview"):
        if mode not in ("draft", "preview", "final"):
            raise RuntimeError("mode must be draft, preview or final")
        if self.render_proc and self.render_proc.poll() is None:
            raise RuntimeError("a render is already running")
        vu = self.p.video_use_dir
        if not vu:
            raise RuntimeError("video-use helpers not found on the editing computer")
        py = vu / ".venv" / "bin" / "python"
        out = self.p.edit_dir / f"{mode}.mp4"
        cmd = [str(py) if py.exists() else sys.executable, str(vu / "helpers" / "render.py"), str(self.p.edl_path), "-o", str(out)]
        if mode in ("draft", "preview"):
            cmd.append(f"--{mode}")
        if self.p.read_edl().get("subtitles"):
            cmd.append("--build-subtitles")
        self.render_proc = subprocess.Popen(cmd, cwd=str(self.p.videos_dir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        threading.Thread(target=self._render_pump, args=(self.render_proc, mode, out), daemon=True).start()
        return {"started": mode}

    def _render_pump(self, proc, mode: str, out: Path):
        self.event("render", {"type": "start", "mode": mode})
        buf, last = [], time.time()
        for line in proc.stdout:
            buf.append(line.rstrip())
            if time.time() - last > 1.0:
                self.event("render", {"type": "log", "line": "\n".join(buf)})
                buf, last = [], time.time()
        proc.wait()
        if buf:
            self.event("render", {"type": "log", "line": "\n".join(buf)})
        ok = proc.returncode == 0 and out.exists()
        rel = str(out.relative_to(self.p.videos_dir)) if ok else None
        if ok:
            try:
                with self.lock:
                    self.upload("file/" + rel, out)
            except Exception as e:
                log("render upload:", e)
        self.event("render", {"type": "done", "ok": ok, "mode": mode, "file": rel, "v": int(out.stat().st_mtime) if ok else 0})
        log(f"render {mode}: {'done' if ok else 'failed'}")

    def rpc_render_stop(self):
        if self.render_proc and self.render_proc.poll() is None:
            self.render_proc.terminate()
        return {}

    def rpc_frame(self, t: float = 0.0):
        off = 0.0
        for r in self.p.read_edl().get("ranges", []):
            d = float(r["end"]) - float(r["start"])
            if off <= t < off + d or r is self.p.read_edl()["ranges"][-1]:
                st = float(r["start"]) + max(0.0, min(d - 0.02, t - off))
                f = self.p.extract_frame(r["source"], st)
                if not f:
                    raise RuntimeError("could not grab that frame")
                return {"jpeg": base64.b64encode(f.read_bytes()).decode(), "source": r["source"], "source_t": round(st, 2)}
            off += d
        raise RuntimeError("the timeline is empty")

    def rpc_asset_clip(self, path: str = "", duration: float = 3):
        r = self.p.asset_clip(path, duration)
        f = self.p.overlay_proxy(r["file"])
        if f:
            with self.lock:
                self.upload("overlay/" + r["file"], f)
        return r

    def rpc_fetch_url(self, url: str = "", name: str = ""):
        name = safe(Path(name).name).lstrip(".")
        if not name or Path(name).suffix.lower() not in MEDIA_EXT:
            raise RuntimeError("name needs a media extension (" + " ".join(sorted(MEDIA_EXT)) + ")")
        dest = self.p.assets_dir / name
        tmp = dest.with_name(dest.name + ".part")
        size = download_public(url, tmp)
        tmp.replace(dest)
        log(f"downloaded {name} ({size / 1e6:.1f} MB)")
        self.publish()
        return {"file": f"assets/{name}", "name": name, "size": size}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def check_public(url: str) -> None:
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or not u.hostname:
        raise RuntimeError("only https URLs are allowed")
    for info in socket.getaddrinfo(u.hostname, u.port or 443, proto=socket.IPPROTO_TCP):
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise RuntimeError("that address is not on the public internet")


def download_public(url: str, dest: Path) -> int:
    """Download a public https file. Each hop is checked so a redirect cannot point at this machine or the LAN."""
    opener = urllib.request.build_opener(NoRedirect)
    for _ in range(5):
        check_public(url)
        req = urllib.request.Request(url, headers={"User-Agent": "video-use-companion"})
        try:
            with opener.open(req, timeout=60) as r:
                ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype.startswith("text/html"):
                    raise RuntimeError("that URL is a web page, not a media file")
                n = 0
                with dest.open("wb") as fh:
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        n += len(chunk)
                        if n > MAX_FETCH:
                            fh.close()
                            dest.unlink(missing_ok=True)
                            raise RuntimeError("file is larger than 500 MB")
                        fh.write(chunk)
                return n
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                url = urllib.parse.urljoin(url, e.headers["Location"])
                continue
            raise RuntimeError(f"download failed: HTTP {e.code}") from None
    raise RuntimeError("too many redirects")


def main():
    ap = argparse.ArgumentParser(description="Publish a video-use project to the cloud Studio and keep it in sync.")
    ap.add_argument("edit_dir")
    ap.add_argument("--cloud", help="Studio URL, e.g. https://video-use-studio.<you>.workers.dev")
    ap.add_argument("--video-use")
    a = ap.parse_args()
    cfg = {}
    cfg_file = Path.home() / ".config" / "video-use" / "cloud.json"
    if cfg_file.exists():
        cfg = json.loads(cfg_file.read_text())
    url = a.cloud or os.environ.get("VIDEO_USE_CLOUD_URL") or cfg.get("url")
    key = os.environ.get("VIDEO_USE_CLOUD_KEY") or cfg.get("sync_key")
    if not url or not key:
        sys.exit(f"Need the Studio URL and sync key: --cloud + VIDEO_USE_CLOUD_KEY, or {cfg_file}")
    if not (url.startswith("https://") or re.match(r"http://(localhost|127\.0\.0\.1)(:\d+)?$", url.rstrip("/"))):
        sys.exit("The Studio URL must be https")
    edit_dir = Path(a.edit_dir).expanduser().resolve()
    if not (edit_dir / "edl.json").exists():
        sys.exit(f"no edl.json in {edit_dir} — run a first video-use draft first")
    comp = Companion(Project(edit_dir, find_video_use(a.video_use)), Cloud(url, key))
    comp.start()
    threading.Thread(target=comp.watch_local, daemon=True).start()
    try:
        comp.poll()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
