"""
On-device image → 3D for Apple Silicon. Nothing leaves this Mac.

Engines
  hunyuan  Hunyuan3D-2mini Turbo (1 photo) / Hunyuan3D-2mv Turbo (2–4 photos) / Hunyuan3D-2 (Ultra).
           Tencent Hunyuan Community License — not licensed for use in the EU, UK or South Korea.

Pipeline: cut-out → shape → cleanup (floaters, degenerate faces, face budget)
          → photo-projection texture (texture_project.py) → GLB in the scan folder.

Weights are safetensors only, fetched into ~/.cache/hy3dgen on first use.
One job runs at a time; the rest wait in a queue.
"""
import gc
import importlib.util
import io
import os
import pathlib
import queue
import sys
import threading
import time
import traceback
import uuid
from typing import Callable, Dict, Optional

ROOT = pathlib.Path(__file__).parent
HUNYUAN_DIR = ROOT / "vendor" / "Hunyuan3D-2"
HY_CACHE = pathlib.Path(os.path.expanduser(os.getenv("HY3DGEN_MODELS", "~/.cache/hy3dgen")))

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
for p in (str(HUNYUAN_DIR),):
    if p not in sys.path:
        sys.path.insert(0, p)

QUALITY = {
    #            Hunyuan grid / steps,  face budget, texture px
    "draft":    {"octree": 256, "steps": 5, "faces": 20000, "tex": 1024},
    "standard": {"octree": 320, "steps": 5, "faces": 60000, "tex": 2048},
    "high":     {"octree": 384, "steps": 8, "faces": 150000, "tex": 2048},
    # Full-size Hunyuan3D-2 (1.1B) for single photos, finest grid, 4K texture.
    "ultra":    {"octree": 448, "steps": 10, "faces": 250000, "tex": 4096, "full": True},
}

HY_MODELS = {
    "single": ("tencent/Hunyuan3D-2mini", "hunyuan3d-dit-v2-mini-turbo", "tencent/Hunyuan3D-2mini", "hunyuan3d-vae-v2-mini-turbo"),
    "multi": ("tencent/Hunyuan3D-2mv", "hunyuan3d-dit-v2-mv-turbo", "tencent/Hunyuan3D-2", "hunyuan3d-vae-v2-0-turbo"),
    "full": ("tencent/Hunyuan3D-2", "hunyuan3d-dit-v2-0-turbo", "tencent/Hunyuan3D-2", "hunyuan3d-vae-v2-0-turbo"),
}

# Which way each engine's raw output faces (camera looks along this axis to see the front).
# Measured with an asymmetric test shape: Hunyuan output faces +Z like normal glTF.
FRONT_FORWARD = (0.0, 0.0, -1.0)
YAW_SIGN = float(os.getenv("LOCAL3D_YAW_SIGN", "1"))

jobs: Dict[str, dict] = {}
_queue: "queue.Queue[str]" = queue.Queue()
_pipes: Dict[str, object] = {}
_worker: Optional[threading.Thread] = None


# ─────────────────────────────────────────────────────────────────────────────
# Availability
# ─────────────────────────────────────────────────────────────────────────────
def _have(*mods) -> bool:
    return all(importlib.util.find_spec(m) for m in mods)


def engines() -> dict:
    import torch
    return {
        "hunyuan": HUNYUAN_DIR.exists() and _have("torch", "diffusers", "pymeshlab", "xatlas", "cv2"),
        "device": "mps" if torch.backends.mps.is_available() else "cpu",
        "hunyuan_multi_downloaded": (HY_CACHE / HY_MODELS["multi"][0] / HY_MODELS["multi"][1] / "model.fp16.safetensors").exists(),
        "hunyuan_single_downloaded": (HY_CACHE / HY_MODELS["single"][0] / HY_MODELS["single"][1] / "model.fp16.safetensors").exists(),
    }


def available() -> bool:
    try:
        e = engines()
        return e["hunyuan"]
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Model loading (safetensors only)
# ─────────────────────────────────────────────────────────────────────────────
def _fetch(repo: str, sub: str) -> None:
    """Download config + safetensors weights into the folder hy3dgen looks in first."""
    from huggingface_hub import hf_hub_download
    dest = HY_CACHE / repo / sub
    dest.mkdir(parents=True, exist_ok=True)
    for f in ("config.yaml", "model.fp16.safetensors"):
        link = dest / f
        if not link.exists():
            path = hf_hub_download(repo, f"{sub}/{f}")
            if link.is_symlink():
                link.unlink()
            link.symlink_to(path)


def _free(keep: Optional[str] = None) -> None:
    import torch
    for k in list(_pipes):
        if k != keep:
            del _pipes[k]
    gc.collect()
    if torch.backends.mps.is_available():
        torch.mps.empty_cache()


def _hunyuan(kind: str, set_stage: Callable):
    import torch
    if kind in _pipes:
        return _pipes[kind]
    _free(keep=kind)
    _unload_chat_model()   # the local chat AI (Ollama) holds ~3 GB; the 3D model needs it more
    low = _available_gb() < 6
    if low:
        _free(keep=None)   # make room: drop every loaded model first
    repo, sub, vae_repo, vae_sub = HY_MODELS[kind]
    set_stage(3, "Downloading Hunyuan3D model (first run only)…" if not (HY_CACHE / repo / sub / "model.fp16.safetensors").exists()
              else ("Loading Hunyuan3D model (low memory, close other apps)…" if low else "Loading Hunyuan3D model…"))
    _fetch(repo, sub)
    _fetch(vae_repo, vae_sub)
    from hy3dgen.shapegen import Hunyuan3DDiTFlowMatchingPipeline
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    pipe = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(
        repo, subfolder=sub, use_safetensors=True, variant="fp16", device=device,
        dtype=torch.float16 if device == "mps" else torch.float32,
    )
    pipe.enable_flashvdm(topk_mode="merge")  # loads the turbo VAE (safetensors)
    _pipes[kind] = pipe
    print(f"[Local3D] Hunyuan3D {kind} loaded on {device}")
    return pipe


def _rgba(image_bytes: bytes):
    import numpy as np
    from PIL import Image
    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    if np.array(img)[..., 3].min() == 255:  # not cut out yet
        from rembg import remove
        img = remove(img).convert("RGBA")
    return img


def _drop_detached(mesh, touch: float = 0.012):
    """Keep the main body plus pieces that touch it; drop blobs floating in the air."""
    import numpy as np
    import trimesh
    from scipy.spatial import cKDTree
    parts = sorted(mesh.split(only_watertight=False), key=lambda p: -len(p.faces))
    if len(parts) <= 1:
        return mesh
    main = parts[0]
    tree = cKDTree(main.vertices[:: max(1, len(main.vertices) // 200000)])
    keep = [main]
    for p in parts[1:]:
        if len(p.faces) >= 0.3 * len(main.faces):
            keep.append(p)
            continue
        sample = p.vertices[:: max(1, len(p.vertices) // 2000)]
        if float(np.min(tree.query(sample)[0])) < touch:
            keep.append(p)
    return trimesh.util.concatenate(keep)


def _cleanup(mesh, faces: int):
    from hy3dgen.shapegen import DegenerateFaceRemover, FaceReducer, FloaterRemover
    mesh = _drop_detached(mesh)
    mesh = FloaterRemover()(mesh)
    mesh = DegenerateFaceRemover()(mesh)
    if len(mesh.faces) > faces:
        mesh = FaceReducer()(mesh, max_facenum=faces)
    return mesh


def _shape_hunyuan(views: dict, q: dict, seed: int, set_stage: Callable):
    import torch
    multi = len(views) > 1
    pipe = _hunyuan("multi" if multi else ("full" if q.get("full") else "single"), set_stage)
    set_stage(20, "Sculpting the shape…")
    image = views if multi else views["front"]
    gen = torch.Generator().manual_seed(seed)
    return pipe(image=image, num_inference_steps=q["steps"], octree_resolution=q["octree"],
                num_chunks=20000, generator=gen, output_type="trimesh")[0]


def _normalize(mesh):
    """Centre on X/Z, sit on Y=0, largest side = 1 unit."""
    import numpy as np
    lo, hi = mesh.bounds
    mesh.apply_translation([-(lo[0] + hi[0]) / 2, -lo[1], -(lo[2] + hi[2]) / 2])
    mesh.apply_scale(1.0 / max(float(np.max(hi - lo)), 1e-6))
    return mesh


def _export_plain(mesh, path: pathlib.Path) -> None:
    import trimesh
    m = trimesh.Trimesh(mesh.vertices, mesh.faces, process=False)
    m.visual = trimesh.visual.TextureVisuals(material=trimesh.visual.material.PBRMaterial(
        baseColorFactor=[200, 200, 205, 255], metallicFactor=0.0, roughnessFactor=0.7))
    m.export(str(path), file_type="glb")


# ─────────────────────────────────────────────────────────────────────────────
# Jobs
# ─────────────────────────────────────────────────────────────────────────────
def _run(job: dict) -> None:
    import trimesh
    from PIL import Image
    from texture_project import project_texture

    def set_stage(p, msg):
        if job.get("abandoned"):
            raise RuntimeError("job was stopped by the watchdog")
        if msg != job.get("stage"):
            job["stage_at"] = time.time()
        job.update(progress=int(p), stage=msg)

    out_dir: pathlib.Path = job["out_dir"]
    q = dict(QUALITY.get(job["quality"], QUALITY["standard"]))
    if job.get("faces"):
        q["faces"] = int(job["faces"])
    if job.get("texture_size"):
        q["tex"] = int(job["texture_size"])
    t0 = time.time()

    views = {k: Image.open(io.BytesIO(v)).convert("RGBA") if job["refine"] else _rgba(v)
             for k, v in job["views"].items()}

    if job["refine"]:  # re-run cleanup + texture on the saved raw shape
        set_stage(10, "Loading saved shape…")
        raw = trimesh.load(str(out_dir / "raw.glb"), force="mesh")
    else:
        set_stage(2, "Preparing photos…")
        for name, img in views.items():
            img.save(out_dir / f"view_{name}.png")
        raw = _shape_hunyuan(views, q, job["seed"], set_stage)
        raw = _normalize(raw)
        raw.export(str(out_dir / "raw.glb"), file_type="glb")

    set_stage(55, "Cleaning up the mesh…")
    mesh = _cleanup(trimesh.Trimesh(raw.vertices, raw.faces, process=True), q["faces"])
    if job.get("smooth"):
        # Light Taubin smoothing: removes the faceted/blocky look without shrinking the model.
        trimesh.smoothing.filter_taubin(mesh, lamb=0.5, nu=-0.53, iterations=int(job["smooth"]))
    _export_plain(mesh, out_dir / "preview.glb")
    job["preview_url"] = f"/scans-files/{out_dir.name}/preview.glb"
    job["events"].append({"type": "preview", "url": job["preview_url"]})

    if job["texture"]:
        textured = project_texture(
            mesh, views, front_forward=FRONT_FORWARD, yaw_sign=YAW_SIGN, size=q["tex"],
            progress=lambda p, msg: set_stage(60 + p * 38, msg),
        )
        textured.export(str(out_dir / job["out_name"]), file_type="glb")
    else:
        _export_plain(mesh, out_dir / job["out_name"])

    job["result"] = {
        "file": job["out_name"],
        "faces": int(len(mesh.faces)),
        "seconds": round(time.time() - t0, 1),
        "engine": job["engine"],
        "quality": job["quality"],
    }
    print(f"[Local3D] ✓ {out_dir.name}/{job['out_name']} in {job['result']['seconds']}s "
          f"({job['result']['faces']} faces, {job['engine']}/{job['quality']})")


def _unload_chat_model() -> None:
    """Ask Ollama to drop any loaded model from memory (it reloads by itself on the next chat)."""
    import json as _json
    import urllib.request
    base = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
    try:
        with urllib.request.urlopen(f"{base}/api/ps", timeout=2) as r:
            loaded = [m["name"] for m in _json.loads(r.read()).get("models", [])]
        for name in loaded:
            req = urllib.request.Request(f"{base}/api/generate", data=_json.dumps({"model": name, "keep_alive": 0}).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
            print(f"[Local3D] Unloaded chat model {name} to free memory")
    except Exception:
        pass


def _available_gb() -> float:
    try:
        import psutil
        return psutil.virtual_memory().available / 1e9
    except Exception:
        return 99.0


def _finish(job: dict) -> None:
    if job.get("finished"):
        return
    job["finished"] = True
    job["views"] = {}
    if job.get("on_done"):
        try:
            job["on_done"](job)
        except Exception:
            traceback.print_exc()


def _work() -> None:
    me = threading.current_thread()
    while True:
        job_id = _queue.get()
        job = jobs[job_id]
        job.update(status="running", stage_at=time.time(), worker=me.name)
        try:
            _run(job)
            if not job.get("abandoned"):
                job.update(status="success", progress=100, stage="Done")
        except Exception as e:
            if not job.get("abandoned"):
                traceback.print_exc()
                job.update(status="failed", error=f"{type(e).__name__}: {e}"[:400])
        finally:
            if not job.get("abandoned"):
                _finish(job)
                # The big models (4-view, Ultra) take ~5 GB each: don't keep them around between jobs.
                if any(k in _pipes for k in ("multi", "full")) and _queue.empty():
                    _free(keep=None)
            _queue.task_done()
        if job.get("abandoned"):
            return   # a replacement worker has already taken over


# How long one step may take before we call it stuck (seconds).
STAGE_LIMITS = {"Loading": 300, "Downloading": 3600, "Sculpting": 900}
DEFAULT_LIMIT = 900


def _watchdog() -> None:
    global _worker
    while True:
        time.sleep(10)
        for job in list(jobs.values()):
            if job["status"] != "running" or job.get("abandoned"):
                continue
            stage = job.get("stage") or ""
            limit = next((v for k, v in STAGE_LIMITS.items() if stage.startswith(k)), DEFAULT_LIMIT)
            stuck = time.time() - job.get("stage_at", time.time())
            if stuck < limit:
                continue
            mins = int(stuck // 60)
            print(f"[Local3D] Watchdog: job {job['id']} stuck on '{stage}' for {mins} min; giving up "
                  f"({_available_gb():.1f} GB RAM free)")
            job.update(status="failed", abandoned=True,
                       error=f"Stuck on \"{stage.rstrip('…')}\" for {mins} min. Your Mac is probably low on memory: "
                             "close big apps (browsers, IDEs) and try again, or use Draft/Standard quality.")
            _finish(job)
            _pipes.clear()
            # The stuck thread may never come back: start a fresh worker for the rest of the queue.
            _worker = threading.Thread(target=_work, daemon=True, name=f"worker-{uuid.uuid4().hex[:4]}")
            _worker.start()


_dog = threading.Thread(target=_watchdog, daemon=True, name="watchdog")
_dog.start()


def start(views: Dict[str, bytes], out_dir: pathlib.Path, *, engine: str = "hunyuan", quality: str = "standard",
          texture: bool = True, faces: Optional[int] = None, texture_size: Optional[int] = None,
          seed: int = 1234, refine: bool = False, out_name: str = "final.glb", smooth: int = 0,
          on_done: Optional[Callable] = None) -> str:
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_work, daemon=True)
        _worker.start()
    job_id = "job-" + uuid.uuid4().hex[:12]
    jobs[job_id] = {
        "id": job_id, "status": "queued", "progress": 0, "stage": "Waiting in queue…", "error": None,
        "views": views, "out_dir": out_dir, "engine": engine, "quality": quality, "texture": texture,
        "faces": faces, "texture_size": texture_size, "seed": seed, "refine": refine,
        "out_name": out_name, "on_done": on_done, "smooth": smooth, "events": [], "result": None, "created": time.time(),
    }
    _queue.put(job_id)
    return job_id


def get(job_id: str) -> Optional[dict]:
    return jobs.get(job_id)


def queue_position(job_id: str) -> int:
    waiting = [j for j in jobs.values() if j["status"] == "queued"]
    waiting.sort(key=lambda j: j["created"])
    return next((i + 1 for i, j in enumerate(waiting) if j["id"] == job_id), 0)
