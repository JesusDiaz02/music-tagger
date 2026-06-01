import asyncio
import json
import os
import platform
import re
import subprocess
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TALB, TDRC
from pydantic import BaseModel
from shazamio import Shazam

BASE_DIR = Path(__file__).parent
CACHE_FILE = BASE_DIR / "music_cache.json"

# Homebrew (ffmpeg) no siempre está en el PATH al lanzar desde el IDE
for _brew_bin in ("/opt/homebrew/bin", "/usr/local/bin"):
    if Path(_brew_bin).is_dir() and _brew_bin not in os.environ.get("PATH", ""):
        os.environ["PATH"] = f"{_brew_bin}:{os.environ.get('PATH', '')}"

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
    except Exception:
        return {}


def save_cache(cache: dict):
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))


def get_tags(path: str) -> dict:
    try:
        tags = ID3(path)
        return {
            "title": str(tags.get("TIT2") or ""),
            "artist": str(tags.get("TPE1") or ""),
            "album": str(tags.get("TALB") or ""),
            "year": str(tags.get("TDRC") or ""),
        }
    except Exception:
        return {"title": "", "artist": "", "album": "", "year": ""}


def format_identify_error(exc: Exception) -> str:
    msg = str(exc).strip()
    low = msg.lower()
    if "ffprobe" in low or "ffmpeg" in low or "avconv" in low:
        return (
            "Falta ffmpeg en tu Mac. Shazam necesita convertir el MP3 antes de escucharlo. "
            "Instálalo en Terminal con: brew install ffmpeg — luego reinicia la app."
        )
    if not msg:
        return repr(exc)
    return msg


def parse_shazam(result: dict) -> Optional[dict]:
    track = result.get("track")
    if not track:
        return None
    album, year = "", ""
    for section in track.get("sections", []):
        if section.get("type") == "SONG":
            for m in section.get("metadata", []):
                key = m.get("title", "").lower()
                if "album" in key:
                    album = m.get("text", "")
                elif "released" in key:
                    year = m.get("text", "")
    return {
        "title": track.get("title", ""),
        "artist": track.get("subtitle", ""),
        "album": album,
        "year": year,
        "cover": track.get("images", {}).get("coverart", ""),
    }


class ScanReq(BaseModel):
    path: str

class IdentifyReq(BaseModel):
    file_path: str

class UpdateReq(BaseModel):
    file_path: str
    title: str = ""
    artist: str = ""
    album: str = ""
    year: str = ""
    rename: bool = False


def _pick_folder_sync() -> str:
    """Abre el diálogo nativo del sistema para elegir una carpeta."""
    system = platform.system()
    if system == "Darwin":
        script = 'POSIX path of (choose folder with prompt "Selecciona la carpeta con tu música")'
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    if system == "Windows":
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askdirectory(title="Selecciona la carpeta con tu música")
            root.destroy()
            return path or ""
        except Exception:
            return ""

    # Linux y otros: zenity o tkinter
    try:
        result = subprocess.run(
            ["zenity", "--file-selection", "--directory", "--title=Selecciona la carpeta con tu música"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except FileNotFoundError:
        pass

    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        path = filedialog.askdirectory(title="Selecciona la carpeta con tu música")
        root.destroy()
        return path or ""
    except Exception:
        return ""


@app.get("/")
async def root():
    return HTMLResponse((BASE_DIR / "index.html").read_text())


@app.post("/pick-folder")
async def pick_folder():
    loop = asyncio.get_event_loop()
    path = await loop.run_in_executor(None, _pick_folder_sync)
    return {"path": path}


@app.post("/scan")
async def scan(
    req: ScanReq,
    offset: int = Query(0),
    limit: int = Query(100),
    filter: str = Query("all"),
):
    p = Path(req.path)
    if not p.is_dir():
        raise HTTPException(404, "Directorio no encontrado")

    cache = load_cache()
    files = []
    counts: dict = {}

    for mp3 in sorted(p.rglob("*.mp3")):
        sp = str(mp3)
        cached = cache.get(sp, {})
        status = cached.get("status", "pending")
        counts[status] = counts.get(status, 0) + 1
        if filter == "all" or status == filter:
            files.append({
                "path": sp,
                "filename": mp3.name,
                "tags": get_tags(sp),
                "identified": cached.get("identified"),
                "status": status,
                "error": cached.get("error"),
            })

    total = len(files)
    return {"files": files[offset:offset + limit], "total": total, "counts": counts}


@app.post("/identify")
async def identify(req: IdentifyReq):
    if not os.path.exists(req.file_path):
        raise HTTPException(404, "Archivo no encontrado")
    try:
        shazam = Shazam()
        result = await shazam.recognize_song(req.file_path)
        identified = parse_shazam(result)
        status = "identified" if identified else "not_found"
        cache = load_cache()
        entry = {"status": status, "identified": identified}
        cache[req.file_path] = entry
        save_cache(cache)
        return {"status": status, "identified": identified, "error": None}
    except Exception as e:
        error_msg = format_identify_error(e)
        cache = load_cache()
        cache[req.file_path] = {
            "status": "error",
            "identified": None,
            "error": error_msg,
        }
        save_cache(cache)
        return {"status": "error", "error": error_msg, "identified": None}


@app.post("/update")
async def update(req: UpdateReq):
    if not os.path.exists(req.file_path):
        raise HTTPException(404, "Archivo no encontrado")
    try:
        try:
            tags = ID3(req.file_path)
        except ID3NoHeaderError:
            tags = ID3()

        for frame in ["TIT2", "TPE1", "TALB", "TDRC"]:
            tags.delall(frame)
        if req.title:  tags.add(TIT2(encoding=3, text=req.title))
        if req.artist: tags.add(TPE1(encoding=3, text=req.artist))
        if req.album:  tags.add(TALB(encoding=3, text=req.album))
        if req.year:   tags.add(TDRC(encoding=3, text=req.year))
        tags.save(req.file_path)

        new_path = req.file_path
        if req.rename and req.title and req.artist:
            old = Path(req.file_path)
            safe = re.sub(r'[\\/:*?"<>|]', "", f"{req.artist} - {req.title}")
            candidate = str(old.parent / f"{safe}.mp3")
            if candidate != req.file_path:
                old.rename(candidate)
                new_path = candidate

        cache = load_cache()
        cache[new_path] = {
            "status": "done",
            "identified": {"title": req.title, "artist": req.artist, "album": req.album, "year": req.year},
        }
        if new_path != req.file_path:
            cache.pop(req.file_path, None)
        save_cache(cache)
        return {"success": True, "new_path": new_path}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/skip")
async def skip(req: IdentifyReq):
    cache = load_cache()
    cache.setdefault(req.file_path, {})["status"] = "skipped"
    save_cache(cache)
    return {"success": True}


@app.get("/audio")
async def audio(path: str):
    if not os.path.exists(path):
        raise HTTPException(404)
    return FileResponse(path, media_type="audio/mpeg")


if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8765)
