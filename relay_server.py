"""
Photo → 3D → Blender, fully on this Mac.

Endpoints:
  POST   /isolate                   Automatic cut-out (rembg)
  POST   /segment                   Tap-to-select cut-out (SAM) from points on the photo
  POST   /generate3d                Photo(s) → 3D job. `image`, or `front` (+ left/back/right)
  GET    /jobs/{id}/stream          SSE progress: progress / preview / complete / error
  GET    /scans                     Gallery
  GET    /scans/{id}                One scan
  PATCH  /scans/{id}                Rename
  DELETE /scans/{id}                Delete
  POST   /scans/{id}/refine         Redo cleanup + texture at a new detail level (no regeneration)
  POST   /scans/{id}/variation      Regenerate from the same photos with a new seed
  POST   /scans/{id}/thumbnail      Save a rendered thumbnail (PNG) from the viewer
  GET    /scans/{id}/export/{fmt}   Download as glb / obj (zip) / stl / ply
  POST   /scans/{id}/ar             AR files (GLB + USDZ) with a baked animation at real-world size
  GET    /engines                   Which local engines + quality presets are available
  POST   /relay                     Forward a model URL to WebSocket clients (Blender)
  WS     /ws?client=X               WebSocket connection for clients

Usage:  python serve.py   (HTTP :8000 for laptop/Blender + HTTPS :8443 for phones)
"""
import asyncio
import io
import json
import os
import pathlib
import shutil
import threading
import time
import uuid
import zipfile
from typing import Dict, List, Optional, Set

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

import ai_local
import ar_export
import local3d

load_dotenv()

import mimetypes
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("application/wasm", ".wasm")
mimetypes.add_type("application/octet-stream", ".task")
mimetypes.add_type("model/vnd.usdz+zip", ".usdz")
mimetypes.add_type("application/manifest+json", ".webmanifest")

BG_REMOVAL = os.getenv("BG_REMOVAL", "rembg").lower()  # rembg | none
REMBG_MODEL = os.getenv("REMBG_MODEL", "isnet-general-use")
SAM_MODEL = os.getenv("SAM_MODEL", "sam")
DEFAULT_ENGINE = "hunyuan"

ROOT = pathlib.Path(__file__).parent
SCANS_DIR = pathlib.Path(os.getenv("SCANS_DIR", str(ROOT / "scans")))
SCANS_DIR.mkdir(parents=True, exist_ok=True)
VIEW_ORDER = ["front", "left", "back", "right"]


class RelayPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    target: str
    url: str
    type: str = "model"
    name: Optional[str] = None
    scan_id: Optional[str] = None


class RenamePayload(BaseModel):
    name: str


class ARPayload(BaseModel):
    animation: str = "idle"
    height_cm: float = 30.0


class ChatPayload(BaseModel):
    message: str
    history: List[Dict[str, str]] = []


class TTSPayload(BaseModel):
    text: str
    voice: Optional[str] = None
    speed: float = 1.0


class RolePayload(BaseModel):
    role: Optional[str] = None          # character | gun | object
    name: Optional[str] = None
    personality: Optional[str] = None
    voice: Optional[str] = None


class EditPayload(BaseModel):
    rotate: List[float] = [0, 0, 0]     # degrees about X, Y, Z
    mirror: bool = False
    brightness: float = 1.0
    contrast: float = 1.0
    saturation: float = 1.0
    hue: float = 0.0                    # degrees
    roughness: Optional[float] = None
    metallic: Optional[float] = None
    smooth: int = 0                     # Taubin smoothing iterations


class RefinePayload(BaseModel):
    quality: Optional[str] = None
    faces: Optional[int] = None
    texture_size: Optional[int] = None
    texture: bool = True


app = FastAPI(title="Photo → 3D → Blender (local)")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

@app.middleware("http")
async def no_stale_ui(request, call_next):
    """Always revalidate the app's own HTML/JS/CSS so phones pick up updates immediately."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".html", ".js", ".css", ".webmanifest")) and not path.startswith("/vendor/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


connections: Dict[str, Set[WebSocket]] = {}
lock = asyncio.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket + relay
# ─────────────────────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, client: str):
    await websocket.accept()
    async with lock:
        connections.setdefault(client, set()).add(websocket)
    print(f"[WS] Client '{client}' connected. Total: {sum(len(v) for v in connections.values())}")
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        async with lock:
            connections.get(client, set()).discard(websocket)
            if connections.get(client) == set():
                connections.pop(client, None)
        print(f"[WS] Client '{client}' disconnected.")


@app.post("/relay")
async def relay(payload: RelayPayload):
    async with lock:
        targets = list(connections.get(payload.target, set()))
    if not targets:
        raise HTTPException(status_code=404, detail="No target clients connected")
    data = payload.model_dump()
    dead: Set[WebSocket] = set()
    for ws in targets:
        try:
            await ws.send_json(data)
        except Exception:
            dead.add(ws)
    if dead:
        async with lock:
            for ws in dead:
                connections.get(payload.target, set()).discard(ws)
    return JSONResponse({"delivered": len(targets) - len(dead)})


# ─────────────────────────────────────────────────────────────────────────────
# Cut-outs: automatic (rembg) and tap-to-select (SAM)
# ─────────────────────────────────────────────────────────────────────────────
_sessions: Dict[str, object] = {}


def _session(name: str):
    from rembg import new_session
    if name not in _sessions:
        print(f"[Isolate] Loading model '{name}' (first run downloads it)...")
        _sessions[name] = new_session(name)
    return _sessions[name]


def run_rembg_isolate(image_bytes: bytes) -> bytes:
    from rembg import remove
    return remove(image_bytes, session=_session(REMBG_MODEL), post_process_mask=True)


def run_sam_segment(image_bytes: bytes, points: List[List[float]]) -> bytes:
    """points: [[x, y, label], ...] in image pixels; label 1 = keep, 0 = remove."""
    import numpy as np
    from PIL import Image
    from rembg import remove

    prompt = [{"type": "point", "data": [int(p[0]), int(p[1])], "label": int(p[2]) if len(p) > 2 else 1}
              for p in points]
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    mask = remove(img, session=_session(SAM_MODEL), sam_prompt=prompt, only_mask=True).convert("L").resize(img.size)
    try:  # keep only the largest blob so stray bits don't end up in the model
        from scipy import ndimage
        arr = np.array(mask) > 127
        labels, n = ndimage.label(arr)
        if n > 1:
            sizes = ndimage.sum(arr, labels, range(1, n + 1))
            keep = ndimage.binary_fill_holes(labels == (int(np.argmax(sizes)) + 1))
            mask = Image.fromarray((keep * 255).astype("uint8"))
    except ImportError:
        pass
    out = img.convert("RGBA")
    out.putalpha(mask)
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


@app.post("/isolate")
async def isolate_object(image: UploadFile = File(...)):
    image_bytes = await image.read()
    try:
        result = await asyncio.to_thread(run_rembg_isolate, image_bytes) if BG_REMOVAL == "rembg" else image_bytes
    except Exception as e:
        print(f"[Isolate] Failed, using original image: {str(e)[:200]}")
        result = image_bytes
    return Response(content=result, media_type="image/png")


@app.post("/segment")
async def segment_object(image: UploadFile = File(...), points: str = Form(...)):
    image_bytes = await image.read()
    try:
        pts = json.loads(points)
        assert isinstance(pts, list) and pts
    except Exception:
        raise HTTPException(status_code=400, detail="points must be a JSON list like [[x, y, 1], ...]")
    try:
        result = await asyncio.to_thread(run_sam_segment, image_bytes, pts)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Segmentation failed: {str(e)[:200]}")
    return Response(content=result, media_type="image/png")


# ─────────────────────────────────────────────────────────────────────────────
# Gallery storage
# ─────────────────────────────────────────────────────────────────────────────
def _scan_dir(scan_id: str) -> pathlib.Path:
    if not scan_id or "/" in scan_id or ".." in scan_id or "\\" in scan_id:
        raise HTTPException(status_code=400, detail="Bad scan id")
    return SCANS_DIR / scan_id


def load_meta(scan_id: str) -> dict:
    path = _scan_dir(scan_id) / "meta.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Scan not found")
    return json.loads(path.read_text())


def save_meta(meta: dict) -> None:
    d = _scan_dir(meta["id"])
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "meta.json.tmp"
    tmp.write_text(json.dumps(meta, indent=2))
    tmp.replace(d / "meta.json")


def update_meta(scan_id: str, **changes) -> dict:
    meta = load_meta(scan_id)
    meta.update(changes)
    save_meta(meta)
    return meta


def public_scan(meta: dict) -> dict:
    base = f"/scans-files/{meta['id']}/"
    model = meta.get("model")
    status = meta.get("status") or ("ready" if model else "failed")
    if status == "processing" and not local3d.get(meta.get("job", "")):
        status = "ready" if model else "failed"  # interrupted by a server restart
    return {
        **meta,
        "status": status,
        "model_url": base + model if model else None,
        "thumb_url": base + meta["thumb"] + f"?v={int(meta.get('updated', 0))}" if meta.get("thumb") else None,
        "views": [v for v in VIEW_ORDER if (_scan_dir(meta["id"]) / f"view_{v}.png").exists()],
    }


@app.get("/scans")
async def list_scans():
    scans = []
    for d in SCANS_DIR.iterdir():
        p = d / "meta.json"
        if d.is_dir() and p.exists():
            try:
                scans.append(public_scan(json.loads(p.read_text())))
            except Exception:
                continue
    scans.sort(key=lambda m: m.get("created", 0), reverse=True)
    return {"scans": scans}


@app.get("/scans/{scan_id}")
async def get_scan(scan_id: str):
    return public_scan(load_meta(scan_id))


@app.patch("/scans/{scan_id}")
async def rename_scan(scan_id: str, payload: RenamePayload):
    name = payload.name.strip()[:80]
    if not name:
        raise HTTPException(status_code=400, detail="Name is empty")
    return public_scan(update_meta(scan_id, name=name))


@app.delete("/scans/{scan_id}")
async def delete_scan(scan_id: str):
    d = _scan_dir(scan_id)
    if not (d / "meta.json").exists():
        raise HTTPException(status_code=404, detail="Scan not found")
    shutil.rmtree(d)
    return {"deleted": scan_id}


@app.post("/scans/{scan_id}/thumbnail")
async def save_thumbnail(scan_id: str, image: UploadFile = File(...)):
    load_meta(scan_id)
    data = await image.read()
    if len(data) > 5_000_000:
        raise HTTPException(status_code=413, detail="Thumbnail too large")
    (_scan_dir(scan_id) / "thumb.png").write_bytes(data)
    return public_scan(update_meta(scan_id, thumb="thumb.png", updated=time.time()))


@app.get("/scans/{scan_id}/export/{fmt}")
async def export_scan(scan_id: str, fmt: str):
    """Download the current model as glb, obj (zip with mtl + texture), stl or ply."""
    meta = load_meta(scan_id)
    d = _scan_dir(scan_id)
    src = d / (meta.get("model") or "final.glb")
    if not src.exists():
        raise HTTPException(status_code=404, detail="Model not ready")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in meta.get("name", scan_id))[:60] or "scan"
    if fmt == "glb":
        return FileResponse(src, filename=f"{safe}.glb", media_type="model/gltf-binary")

    def build() -> pathlib.Path:
        import trimesh
        mesh = trimesh.load(str(src), force="mesh")
        out = d / "exports"
        out.mkdir(exist_ok=True)
        if fmt == "stl":  # 3D printing: millimetres, 100 mm tall by default
            m = trimesh.Trimesh(mesh.vertices * 100.0, mesh.faces)
            path = out / f"{safe}.stl"
            m.export(str(path))
        elif fmt == "ply":
            path = out / f"{safe}.ply"
            mesh.export(str(path))
        elif fmt == "obj":
            folder = out / "obj"
            shutil.rmtree(folder, ignore_errors=True)
            folder.mkdir()
            mesh.export(str(folder / f"{safe}.obj"), file_type="obj")
            path = out / f"{safe}_obj.zip"
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
                for f in folder.iterdir():
                    z.write(f, f.name)
        else:
            raise HTTPException(status_code=400, detail="Format must be glb, obj, stl or ply")
        return path

    path = await asyncio.to_thread(build)
    media = {"stl": "model/stl", "ply": "application/octet-stream", "obj": "application/zip"}[fmt]
    return FileResponse(path, filename=path.name, media_type=media)


# ─────────────────────────────────────────────────────────────────────────────
# Generation
# ─────────────────────────────────────────────────────────────────────────────
def _on_done(scan_id: str):
    def done(job: dict) -> None:
        try:
            meta = load_meta(scan_id)
        except HTTPException:
            return
        if job["status"] == "success":
            r = job["result"]
            if meta.get("model") and meta.get("model") != r["file"]:
                meta.setdefault("model_stack", []).append(meta["model"])
            meta.update(model=r["file"], status="ready", faces=r["faces"], seconds=r["seconds"],
                        engine=r["engine"], quality=r["quality"], updated=time.time())
            meta.setdefault("history", []).append({**r, "at": time.time()})
            if not meta.get("ai"):
                threading.Thread(target=_identify_bg, args=(scan_id,), daemon=True).start()
        elif not meta.get("model"):
            meta.update(status="failed", error=job["error"])
        save_meta(meta)
    return done


def _check_engine(engine: str = "hunyuan") -> str:
    """Hunyuan3D-2 is the only engine; everything runs on this Mac."""
    if not local3d.engines().get("hunyuan"):
        raise HTTPException(status_code=400, detail="The local 3D engine isn't installed. See README.")
    return "hunyuan"


@app.post("/generate3d")
async def generate_3d(
    image: Optional[UploadFile] = File(None),
    front: Optional[UploadFile] = File(None),
    left: Optional[UploadFile] = File(None),
    back: Optional[UploadFile] = File(None),
    right: Optional[UploadFile] = File(None),
    engine: str = Form(DEFAULT_ENGINE),
    quality: str = Form("standard"),
    texture: bool = Form(True),
    seed: int = Form(1234),
    name: Optional[str] = Form(None),
):
    if quality not in local3d.QUALITY:
        raise HTTPException(status_code=400, detail=f"quality must be one of {list(local3d.QUALITY)}")
    engine = _check_engine(engine)
    files = {"front": front or image, "left": left, "back": back, "right": right}
    views = {k: await f.read() for k, f in files.items() if f is not None}
    if "front" not in views:
        raise HTTPException(status_code=400, detail="Send `image`, or `front` (+ other views)")

    scan_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    kind = "multiview" if len(views) > 1 else "photo"
    meta = {
        "id": scan_id, "kind": kind, "created": time.time(), "updated": time.time(),
        "name": (name or f"{kind}-{scan_id[-6:]}")[:80], "status": "processing",
        "engine": engine, "quality": quality, "seed": seed, "texture": texture,
        "model": None, "thumb": "view_front.png",
    }
    save_meta(meta)
    job_id = local3d.start(views, _scan_dir(scan_id), engine=engine, quality=quality, texture=texture,
                           seed=seed, on_done=_on_done(scan_id))
    update_meta(scan_id, job=job_id)
    return {"scan_id": scan_id, "job_id": job_id, "engine": engine}


def _saved_views(scan_id: str) -> Dict[str, bytes]:
    d = _scan_dir(scan_id)
    views = {v: (d / f"view_{v}.png").read_bytes() for v in VIEW_ORDER if (d / f"view_{v}.png").exists()}
    if "front" not in views:
        raise HTTPException(status_code=400, detail="This scan has no saved photos")
    return views


@app.post("/scans/{scan_id}/refine")
async def refine_scan(scan_id: str, payload: RefinePayload):
    """Re-run cleanup + texture with a new face budget / texture size, reusing the saved shape."""
    meta = load_meta(scan_id)
    if not (_scan_dir(scan_id) / "raw.glb").exists():
        raise HTTPException(status_code=400, detail="This scan has no saved shape to refine")
    quality = payload.quality or meta.get("quality", "standard")
    out_name = f"refined_{int(time.time())}.glb"
    job_id = local3d.start(_saved_views(scan_id), _scan_dir(scan_id), engine=meta.get("engine", "hunyuan"),
                           quality=quality, texture=payload.texture, faces=payload.faces,
                           texture_size=payload.texture_size, refine=True, out_name=out_name,
                           on_done=_on_done(scan_id))
    return {"scan_id": scan_id, "job_id": job_id}


@app.post("/scans/{scan_id}/variation")
async def variation_scan(scan_id: str):
    """Same photos, new random seed → a different take on the shape (Hunyuan)."""
    meta = load_meta(scan_id)
    views = _saved_views(scan_id)
    seed = int(meta.get("seed", 1234)) + 1 + int(time.time()) % 997
    fake_uploads = {k: v for k, v in views.items()}
    new_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    engine = _check_engine(meta.get("engine", DEFAULT_ENGINE))
    new_meta = {
        "id": new_id, "kind": meta.get("kind", "photo"), "created": time.time(), "updated": time.time(),
        "name": f"{meta.get('name', 'scan')} (variation)"[:80], "status": "processing",
        "engine": engine, "quality": meta.get("quality", "standard"), "seed": seed,
        "texture": meta.get("texture", True), "model": None, "thumb": "view_front.png", "parent": scan_id,
    }
    save_meta(new_meta)
    job_id = local3d.start(fake_uploads, _scan_dir(new_id), engine=engine, quality=new_meta["quality"],
                           texture=new_meta["texture"], seed=seed, on_done=_on_done(new_id))
    update_meta(new_id, job=job_id)
    return {"scan_id": new_id, "job_id": job_id, "engine": engine}


@app.get("/ar/presets")
async def ar_presets():
    return {"animations": ar_export.preset_names()}


@app.post("/scans/{scan_id}/ar")
async def ar_assets(scan_id: str, payload: ARPayload):
    """GLB (Android / WebXR) + USDZ (iPhone Quick Look) with a baked animation at real-world size."""
    meta = load_meta(scan_id)
    d = _scan_dir(scan_id)
    src = d / (meta.get("model") or "final.glb")
    if not src.exists():
        raise HTTPException(status_code=404, detail="Model not ready")
    ar_dir = d / "ar"
    ar_dir.mkdir(exist_ok=True)
    try:
        r = await asyncio.to_thread(ar_export.make_ar_assets, src, ar_dir, payload.animation, payload.height_cm)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AR export failed: {str(e)[:200]}")
    base = f"/scans-files/{scan_id}/ar/"
    return {"glb_url": base + r["glb"], "usdz_url": base + r["usdz"], "animation": r["preset"],
            "height_cm": r["height_cm"], "model_url": f"/scans-files/{scan_id}/{src.name}", "name": meta.get("name")}


# ─────────────────────────────────────────────────────────────────────────────
# Local AI: identify, chat, speech
# ─────────────────────────────────────────────────────────────────────────────
def _identify_bg(scan_id: str) -> Optional[dict]:
    try:
        img = _scan_dir(scan_id) / "view_front.png"
        if not img.exists():
            return None
        info = ai_local.identify(img.read_bytes())
        meta = load_meta(scan_id)
        meta["ai"] = info
        if meta.get("name", "").startswith(("photo-", "multiview-")):
            meta["name"] = info.get("name", meta["name"])[:80]
        save_meta(meta)
        print(f"[AI] {scan_id}: {info.get('name')} · {info.get('role')}")
        return info
    except Exception as e:
        print(f"[AI] identify failed for {scan_id}: {e}")
        return None


@app.get("/ai/status")
async def ai_status():
    return await asyncio.to_thread(ai_local.status)


@app.post("/scans/{scan_id}/identify")
async def identify_scan(scan_id: str):
    load_meta(scan_id)
    info = await asyncio.to_thread(_identify_bg, scan_id)
    if info is None:
        raise HTTPException(status_code=503, detail="Local AI isn't available. Start Ollama and pull gemma3:4b.")
    return public_scan(load_meta(scan_id))


@app.post("/scans/{scan_id}/role")
async def set_role(scan_id: str, payload: RolePayload):
    meta = load_meta(scan_id)
    ai = meta.get("ai") or {"name": meta.get("name", "Scan"), "personality": "Cheerful and curious.", "voice": "cute"}
    for k in ("role", "name", "personality", "voice"):
        v = getattr(payload, k)
        if v:
            ai[k] = v
    meta["ai"] = ai
    save_meta(meta)
    return public_scan(meta)


@app.post("/scans/{scan_id}/chat")
async def chat_scan(scan_id: str, payload: ChatPayload):
    meta = load_meta(scan_id)
    ai = meta.get("ai") or {"name": meta.get("name", "Scan"), "what": "a toy", "personality": "Cheerful and curious."}
    try:
        return await asyncio.to_thread(ai_local.chat, ai, payload.message, payload.history)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Local AI isn't available ({str(e)[:120]}). Is Ollama running?")


@app.post("/ai/stt")
async def speech_to_text(audio: UploadFile = File(...)):
    data = await audio.read()
    if len(data) > 25_000_000:
        raise HTTPException(status_code=413, detail="Recording too long")
    try:
        return {"text": await asyncio.to_thread(ai_local.transcribe, data)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Speech recognition failed: {str(e)[:160]}")


@app.post("/ai/tts")
async def text_to_speech(payload: TTSPayload):
    try:
        wav = await asyncio.to_thread(ai_local.speak, payload.text, payload.voice or "cute", payload.speed)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Speech failed: {str(e)[:160]}")
    return Response(content=wav, media_type="audio/wav")


# ─────────────────────────────────────────────────────────────────────────────
# Model editor
# ─────────────────────────────────────────────────────────────────────────────
def _apply_edit(src: pathlib.Path, dst: pathlib.Path, e: EditPayload) -> int:
    import numpy as np
    import trimesh
    from PIL import Image, ImageEnhance

    mesh = trimesh.load(str(src), force="mesh")
    rx, ry, rz = (np.radians(float(a)) for a in e.rotate)
    for angle, axis in ((rx, [1, 0, 0]), (ry, [0, 1, 0]), (rz, [0, 0, 1])):
        if angle:
            mesh.apply_transform(trimesh.transformations.rotation_matrix(angle, axis))
    if e.mirror:
        mesh.apply_transform(np.diag([-1.0, 1, 1, 1]))
        mesh.faces = mesh.faces[:, ::-1]
    if e.smooth > 0:
        trimesh.smoothing.filter_taubin(mesh, iterations=int(min(e.smooth, 30)))
    lo, hi = mesh.bounds  # back on the ground, centred
    mesh.apply_translation([-(lo[0] + hi[0]) / 2, -lo[1], -(lo[2] + hi[2]) / 2])

    mat = getattr(mesh.visual, "material", None)
    img = getattr(mat, "baseColorTexture", None) if mat is not None else None
    if img is not None:
        img = img.convert("RGB")
        if e.brightness != 1:
            img = ImageEnhance.Brightness(img).enhance(e.brightness)
        if e.contrast != 1:
            img = ImageEnhance.Contrast(img).enhance(e.contrast)
        if e.saturation != 1:
            img = ImageEnhance.Color(img).enhance(e.saturation)
        if e.hue:
            hsv = np.array(img.convert("HSV")).astype(np.int16)
            hsv[..., 0] = (hsv[..., 0] + int(e.hue / 360 * 255)) % 256
            img = Image.fromarray(hsv.astype(np.uint8), "HSV").convert("RGB")
        mat.baseColorTexture = img
    if mat is not None:
        if e.roughness is not None:
            mat.roughnessFactor = float(min(max(e.roughness, 0), 1))
        if e.metallic is not None:
            mat.metallicFactor = float(min(max(e.metallic, 0), 1))
    mesh.export(str(dst), file_type="glb")
    return int(len(mesh.faces))


@app.post("/scans/{scan_id}/edit")
async def edit_scan(scan_id: str, payload: EditPayload):
    meta = load_meta(scan_id)
    d = _scan_dir(scan_id)
    src = d / (meta.get("model") or "final.glb")
    if not src.exists():
        raise HTTPException(status_code=404, detail="Model not ready")
    name = f"edited_{int(time.time() * 1000)}.glb"
    faces = await asyncio.to_thread(_apply_edit, src, d / name, payload)
    meta.setdefault("model_stack", []).append(meta.get("model"))
    meta.update(model=name, faces=faces, updated=time.time())
    save_meta(meta)
    return public_scan(meta)


@app.post("/scans/{scan_id}/undo")
async def undo_scan(scan_id: str):
    meta = load_meta(scan_id)
    stack = meta.get("model_stack") or []
    if not stack:
        raise HTTPException(status_code=400, detail="Nothing to undo")
    meta["model"] = stack.pop()
    meta["model_stack"] = stack
    meta["updated"] = time.time()
    save_meta(meta)
    return public_scan(meta)


@app.get("/addon.zip")
async def addon_zip():
    return FileResponse(ROOT / "ws_import_addon.zip", filename="camera_to_blender_addon.zip", media_type="application/zip")


@app.get("/jobs/{job_id}/stream")
async def job_stream(job_id: str, scan_id: Optional[str] = None):
    async def gen():
        last, sent_events = None, 0
        while True:
            job = local3d.get(job_id)
            if job is None:
                yield f"event: error\ndata: {json.dumps({'error': 'Job not found (server restarted?)'})}\n\n"
                return
            while sent_events < len(job["events"]):
                ev = job["events"][sent_events]
                sent_events += 1
                yield f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n"
            pos = local3d.queue_position(job_id)
            state = (job["status"], job["progress"], job["stage"], pos)
            if state != last:
                last = state
                stage = f"Waiting in queue (#{pos})…" if pos else job["stage"]
                yield f"event: progress\ndata: {json.dumps({'progress': job['progress'], 'status': job['status'], 'stage': stage})}\n\n"
            if job["status"] == "success":
                sid = scan_id or job["out_dir"].name
                r = job["result"]
                done = {"status": "success", "local_url": f"/scans-files/{sid}/{r['file']}", **r}
                yield f"event: complete\ndata: {json.dumps(done)}\n\n"
                return
            if job["status"] == "failed":
                yield f"event: error\ndata: {json.dumps({'error': job['error']})}\n\n"
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/engines")
async def engines_info():
    info = await asyncio.to_thread(local3d.engines)
    return {**info, "default": DEFAULT_ENGINE, "quality": local3d.QUALITY}


# ─────────────────────────────────────────────────────────────────────────────
# Health + connection info
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "bg_removal": BG_REMOVAL, "local3d_available": local3d.available(),
            "connected_clients": {k: len(v) for k, v in connections.items()}}


def lan_ip() -> Optional[str]:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


@app.get("/connection-info")
async def connection_info():
    http_port = int(os.getenv("PORT", "8000"))
    https_port = int(os.getenv("HTTPS_PORT", "8443"))
    ip = lan_ip()
    async with lock:
        clients = {k: len(v) for k, v in connections.items()}
    return {
        "ws_url": f"ws://localhost:{http_port}/ws?client=blender",
        "connection_type": "local",
        "phone_url": f"https://{ip}:{https_port}" if ip else None,
        "clients": clients,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Static files
# ─────────────────────────────────────────────────────────────────────────────
app.mount("/scans-files", StaticFiles(directory=str(SCANS_DIR)), name="scans")
webapp_dir = ROOT / "webapp"
if webapp_dir.exists():
    app.mount("/", StaticFiles(directory=str(webapp_dir), html=True), name="webapp")
