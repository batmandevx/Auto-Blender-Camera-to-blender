"""
AR assets for a scan:
  • GLB with a baked, looping animation + real-world scale  (Android Scene Viewer / WebXR / viewer)
  • USDZ with the same animation + texture                  (iPhone / iPad AR Quick Look)

Models are stored 1 unit tall-ish with their feet at Y=0, facing +Z, so every animation
pivots naturally at the base.
"""
import math
import pathlib
import shutil
import tempfile
from typing import Callable, Dict, List, Tuple

import numpy as np

FPS = 30


def _quat_from_euler(x: float, y: float, z: float) -> Tuple[float, float, float, float]:
    """XYZ intrinsic Euler (radians) → quaternion (x, y, z, w)."""
    cx, sx = math.cos(x / 2), math.sin(x / 2)
    cy, sy = math.cos(y / 2), math.sin(y / 2)
    cz, sz = math.cos(z / 2), math.sin(z / 2)
    return (sx * cy * cz - cx * sy * sz, cx * sy * cz + sx * cy * sz,
            cx * cy * sz - sx * sy * cz, cx * cy * cz + sx * sy * sz)


def _hop(t: float) -> float:
    return abs(math.sin(math.pi * t))


# Each preset: (duration seconds, fn(t in 0..1) -> (translate xyz, euler xyz radians, scale xyz))
Pose = Tuple[Tuple[float, float, float], Tuple[float, float, float], Tuple[float, float, float]]
PRESETS: Dict[str, Tuple[float, Callable[[float], Pose]]] = {
    "idle": (2.4, lambda t: ((0, 0, 0), (0, 0, 0),
                             (1 - 0.012 * math.sin(2 * math.pi * t), 1 + 0.03 * math.sin(2 * math.pi * t),
                              1 - 0.012 * math.sin(2 * math.pi * t)))),
    "spin": (3.0, lambda t: ((0, 0, 0), (0, 2 * math.pi * t, 0), (1, 1, 1))),
    "bounce": (0.9, lambda t: ((0, 0.28 * _hop(t), 0), (0, 0, 0),
                               (1 + 0.08 * (1 - _hop(t)) ** 6, 1 - 0.12 * (1 - _hop(t)) ** 6 + 0.04 * _hop(t),
                                1 + 0.08 * (1 - _hop(t)) ** 6))),
    "hop": (1.6, lambda t: ((0, 0.45 * _hop(t), 0), (0, 2 * math.pi * min(max((t - 0.1) / 0.8, 0), 1), 0), (1, 1, 1))),
    "wobble": (1.2, lambda t: ((0, 0, 0), (0, 0, math.radians(12) * math.sin(2 * math.pi * t)), (1, 1, 1))),
    "float": (4.0, lambda t: ((0, 0.15 + 0.05 * math.sin(2 * math.pi * t), 0), (0, 2 * math.pi * t, 0), (1, 1, 1))),
    "dance": (2.0, lambda t: ((0, 0.06 * _hop(2 * t), 0),
                              (0, math.radians(25) * math.sin(2 * math.pi * t), math.radians(10) * math.sin(4 * math.pi * t)),
                              (1, 1, 1))),
    "walk": (0.8, lambda t: ((0, 0.04 * _hop(2 * t), 0), (0, 0, math.radians(7) * math.sin(2 * math.pi * t)), (1, 1, 1))),
    "run": (0.5, lambda t: ((0, 0.09 * _hop(2 * t), 0), (math.radians(8), 0, math.radians(9) * math.sin(2 * math.pi * t)), (1, 1, 1))),
    "wave": (1.6, lambda t: ((0, 0.02 * _hop(4 * t), 0), (0, 0, math.radians(14) * math.sin(6 * math.pi * t)), (1, 1, 1))),
    "nod": (1.2, lambda t: ((0, 0, 0), (math.radians(12) * math.sin(4 * math.pi * t), 0, 0), (1, 1, 1))),
    "shake": (1.2, lambda t: ((0, 0, 0), (0, math.radians(22) * math.sin(6 * math.pi * t), 0), (1, 1, 1))),
    "backflip": (1.4, lambda t: ((0, 0.6 * _hop(t), 0), (-2 * math.pi * min(max((t - 0.1) / 0.75, 0), 1), 0, 0), (1, 1, 1))),
    "celebrate": (1.8, lambda t: ((0, 0.3 * _hop(3 * t), 0), (0, 2 * math.pi * t, math.radians(8) * math.sin(6 * math.pi * t)),
                                  (1 + 0.05 * _hop(3 * t), 1 + 0.05 * _hop(3 * t), 1 + 0.05 * _hop(3 * t)))),
    "sleep": (3.0, lambda t: ((0, 0, 0), (0, 0, math.radians(-14) + math.radians(2) * math.sin(2 * math.pi * t)),
                              (1 + 0.02 * math.sin(2 * math.pi * t), 1 - 0.03 * math.sin(2 * math.pi * t), 1))),
    "think": (2.4, lambda t: ((0, 0, 0), (math.radians(5), math.radians(15) * math.sin(2 * math.pi * t), math.radians(6)), (1, 1, 1))),
    "swim": (2.0, lambda t: ((0, 0.2 + 0.05 * math.sin(2 * math.pi * t), 0),
                             (0, math.radians(18) * math.sin(2 * math.pi * t), math.radians(10) * math.sin(4 * math.pi * t)), (1, 1, 1))),
    "pulse": (0.9, lambda t: ((0, 0, 0), (0, 0, 0), tuple([1 + 0.08 * max(math.sin(2 * math.pi * t * 2), 0) ** 3] * 3))),
}


def _samples(preset: str):
    dur, fn = PRESETS[preset]
    n = max(2, int(dur * FPS) + 1)
    times = np.linspace(0, dur, n, dtype=np.float32)
    poses = [fn(i / (n - 1)) for i in range(n)]
    tr = np.array([p[0] for p in poses], np.float32)
    rot = np.array([_quat_from_euler(*p[1]) for p in poses], np.float32)
    sc = np.array([p[2] for p in poses], np.float32)
    return times, tr, rot, sc


# ─────────────────────────────────────────────────────────────────────────────
# GLB
# ─────────────────────────────────────────────────────────────────────────────
def bake_glb(src: pathlib.Path, dst: pathlib.Path, preset: str, scale: float) -> None:
    """Wrap the scene in scale + animated nodes and add a looping animation."""
    import pygltflib as G

    g = G.GLTF2().load(str(src))
    blob = bytearray(g.binary_blob() or b"")

    def add(data: np.ndarray, typ: str, with_minmax: bool = False) -> int:
        nonlocal blob
        while len(blob) % 4:
            blob += b"\x00"
        offset = len(blob)
        raw = data.astype(np.float32).tobytes()
        blob += raw
        g.bufferViews.append(G.BufferView(buffer=0, byteOffset=offset, byteLength=len(raw)))
        acc = G.Accessor(bufferView=len(g.bufferViews) - 1, componentType=G.FLOAT, count=len(data), type=typ)
        if with_minmax:
            acc.min, acc.max = [float(data.min())], [float(data.max())]
        g.accessors.append(acc)
        return len(g.accessors) - 1

    scene = g.scenes[g.scene or 0]
    anim_idx = len(g.nodes)
    g.nodes.append(G.Node(name="ar_anim", children=list(scene.nodes)))
    g.nodes.append(G.Node(name="ar_scale", children=[anim_idx], scale=[scale] * 3))
    scene.nodes = [anim_idx + 1]

    if preset and preset != "none":
        times, tr, rot, sc = _samples(preset)
        t_acc = add(times, G.SCALAR, with_minmax=True)
        samplers, channels = [], []
        for path, arr, typ in (("translation", tr, G.VEC3), ("rotation", rot, G.VEC4), ("scale", sc, G.VEC3)):
            samplers.append(G.AnimationSampler(input=t_acc, output=add(arr, typ), interpolation="LINEAR"))
            channels.append(G.AnimationChannel(sampler=len(samplers) - 1,
                                               target=G.AnimationChannelTarget(node=anim_idx, path=path)))
        g.animations = [G.Animation(name=preset, samplers=samplers, channels=channels)]

    if not g.buffers:
        g.buffers.append(G.Buffer())
    g.buffers[0].byteLength = len(blob)
    g.set_binary_blob(bytes(blob))
    g.save_binary(str(dst))


# ─────────────────────────────────────────────────────────────────────────────
# USDZ (iOS AR Quick Look)
# ─────────────────────────────────────────────────────────────────────────────
def build_usdz(src: pathlib.Path, dst: pathlib.Path, preset: str, scale: float) -> None:
    import trimesh
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade, UsdUtils, Vt

    mesh = trimesh.load(str(src), force="mesh")
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="usdz_"))
    try:
        usdc = tmp / "model.usdc"
        stage = Usd.Stage.CreateNew(str(usdc))
        UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
        UsdGeom.SetStageMetersPerUnit(stage, 1.0)
        root = UsdGeom.Xform.Define(stage, "/Root")
        stage.SetDefaultPrim(root.GetPrim())
        scaler = UsdGeom.Xform.Define(stage, "/Root/Scale")
        scaler.AddScaleOp().Set(Gf.Vec3f(scale, scale, scale))
        anim = UsdGeom.Xform.Define(stage, "/Root/Scale/Anim")

        if preset and preset != "none":
            times, tr, rot, sc = _samples(preset)
            stage.SetTimeCodesPerSecond(FPS)
            stage.SetStartTimeCode(0)
            stage.SetEndTimeCode(len(times) - 1)
            t_op, o_op, s_op = anim.AddTranslateOp(), anim.AddOrientOp(), anim.AddScaleOp()
            for i in range(len(times)):
                t_op.Set(Gf.Vec3d(*map(float, tr[i])), i)
                x, y, z, w = map(float, rot[i])
                o_op.Set(Gf.Quatf(w, x, y, z), i)
                s_op.Set(Gf.Vec3f(*map(float, sc[i])), i)

        geo = UsdGeom.Mesh.Define(stage, "/Root/Scale/Anim/Model")
        geo.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(mesh.vertices.astype(np.float32)))
        geo.CreateFaceVertexCountsAttr(Vt.IntArray([3] * len(mesh.faces)))
        geo.CreateFaceVertexIndicesAttr(Vt.IntArray.FromNumpy(mesh.faces.astype(np.int32).ravel()))
        geo.CreateNormalsAttr(Vt.Vec3fArray.FromNumpy(mesh.vertex_normals.astype(np.float32)))
        geo.SetNormalsInterpolation(UsdGeom.Tokens.vertex)
        geo.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
        lo, hi = mesh.bounds
        geo.CreateExtentAttr(Vt.Vec3fArray([Gf.Vec3f(*map(float, lo)), Gf.Vec3f(*map(float, hi))]))

        mat = UsdShade.Material.Define(stage, "/Root/Materials/Mat")
        shader = UsdShade.Shader.Define(stage, "/Root/Materials/Mat/Surface")
        shader.CreateIdAttr("UsdPreviewSurface")
        shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.75)
        shader.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(0.0)
        mat.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")

        uv = getattr(mesh.visual, "uv", None)
        material = getattr(mesh.visual, "material", None)
        image = getattr(material, "baseColorTexture", None) if material is not None else None
        if uv is not None and image is not None:
            image.convert("RGB").save(tmp / "texture.png")
            st = UsdGeom.PrimvarsAPI(geo).CreatePrimvar("st", Sdf.ValueTypeNames.TexCoord2fArray,
                                                         UsdGeom.Tokens.vertex)
            st.Set(Vt.Vec2fArray.FromNumpy(np.asarray(uv, np.float32)))
            reader = UsdShade.Shader.Define(stage, "/Root/Materials/Mat/STReader")
            reader.CreateIdAttr("UsdPrimvarReader_float2")
            reader.CreateInput("varname", Sdf.ValueTypeNames.Token).Set("st")
            tex = UsdShade.Shader.Define(stage, "/Root/Materials/Mat/Texture")
            tex.CreateIdAttr("UsdUVTexture")
            tex.CreateInput("file", Sdf.ValueTypeNames.Asset).Set("texture.png")
            tex.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(reader.ConnectableAPI(), "result")
            tex.CreateInput("wrapS", Sdf.ValueTypeNames.Token).Set("repeat")
            tex.CreateInput("wrapT", Sdf.ValueTypeNames.Token).Set("repeat")
            tex.CreateOutput("rgb", Sdf.ValueTypeNames.Float3)
            shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(tex.ConnectableAPI(), "rgb")
        else:
            shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(0.78, 0.78, 0.8))

        UsdShade.MaterialBindingAPI.Apply(geo.GetPrim()).Bind(mat)
        stage.GetRootLayer().Save()
        ok = UsdUtils.CreateNewARKitUsdzPackage(Sdf.AssetPath(str(usdc)), str(dst))
        if not ok:
            raise RuntimeError("USDZ packaging failed")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def model_height(src: pathlib.Path) -> float:
    import trimesh
    m = trimesh.load(str(src), force="mesh")
    return float(max(m.bounds[1][1] - m.bounds[0][1], 1e-6))


def make_ar_assets(src: pathlib.Path, out_dir: pathlib.Path, preset: str, height_cm: float) -> Dict[str, str]:
    """Build (or reuse) GLB + USDZ for this preset/size. Returns file names."""
    preset = preset if preset in PRESETS else "none"
    height_cm = float(min(max(height_cm, 2), 500))
    stem = f"ar_{preset}_{int(round(height_cm))}cm_{src.stem}"
    glb, usdz = out_dir / f"{stem}.glb", out_dir / f"{stem}.usdz"
    scale = (height_cm / 100.0) / model_height(src)
    if not glb.exists():
        bake_glb(src, glb, preset, scale)
    if not usdz.exists():
        build_usdz(src, usdz, preset, scale)
    return {"glb": glb.name, "usdz": usdz.name, "preset": preset, "height_cm": height_cm}


def preset_names() -> List[str]:
    return ["none", *PRESETS.keys()]
