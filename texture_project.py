"""
Photo-projection texturing: paints your photo(s) onto a generated mesh.

1. UV-unwrap the mesh (xatlas).
2. For every texel, find its 3D point + normal.
3. For each photo (front / left / back / right), project the texel into the photo with an
   orthographic camera, check it isn't hidden behind other geometry, and blend colours,
   weighting surfaces that face the camera most.
4. Fill what no photo sees (fallback colours, then inpainting), and pad UV seams.

All CPU/numpy, no GPU rasteriser needed, so it runs on any Mac.
"""
from typing import Dict, Optional, Tuple

import cv2
import numpy as np
import trimesh
import xatlas
from PIL import Image, ImageDraw

# Yaw (degrees, about +Y) of each capture view relative to the front view.
VIEW_YAW = {"front": 0.0, "left": 90.0, "back": 180.0, "right": 270.0}


def _rot_y(deg: float) -> np.ndarray:
    a = np.radians(deg)
    c, s = np.cos(a), np.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def view_basis(front_forward: np.ndarray, yaw: float, yaw_sign: float = 1.0):
    """Camera basis for a view. `forward` points from the camera into the scene."""
    f = _rot_y(yaw * yaw_sign) @ np.asarray(front_forward, float)
    f /= np.linalg.norm(f)
    up = np.array([0.0, 1.0, 0.0])
    r = np.cross(f, up)
    r /= np.linalg.norm(r)
    u = np.cross(r, f)
    return r, u, f


def _alpha_bbox(alpha: np.ndarray) -> Optional[Tuple[float, float, float, float]]:
    ys, xs = np.where(alpha > 127)
    if len(xs) < 16:
        return None
    return float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max())


class ViewProjector:
    """Maps 3D points to pixels of one photo, fitted so the mesh silhouette matches the cut-out."""

    def __init__(self, verts: np.ndarray, image: Image.Image, basis, faces: Optional[np.ndarray] = None):
        self.r, self.u, self.f = basis
        rgba = np.array(image.convert("RGBA"))
        self.rgb = rgba[..., :3].astype(np.float32)
        self.alpha = rgba[..., 3]
        self.h, self.w = self.alpha.shape
        x, y = verts @ self.r, verts @ self.u
        # Robust starting guess: percentile bounds, so a few stray vertices can't stretch the box.
        mx0, mx1 = np.percentile(x, [0.5, 99.5])
        my0, my1 = np.percentile(y, [0.5, 99.5])
        bb = _alpha_bbox(self.alpha) or (0, 0, self.w - 1, self.h - 1)
        iw, ih = bb[2] - bb[0], bb[3] - bb[1]
        mw, mh = max(mx1 - mx0, 1e-6), max(my1 - my0, 1e-6)
        self.sx = self.sy = 0.5 * (iw / mw + ih / mh)
        self.mc = np.array([(mx0 + mx1) / 2, (my0 + my1) / 2])
        self.ic = np.array([(bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2])
        if faces is not None:
            self.iou = self._refine(x, y, faces)

    def _refine(self, x, y, faces, res: int = 192) -> float:
        """Search scale (x and y) and offset that make the mesh outline overlap the photo's cut-out best."""
        # Mesh silhouette on a fixed grid in mesh coordinates.
        x0, x1, y0, y1 = x.min(), x.max(), y.min(), y.max()
        pad = 0.08 * max(x1 - x0, y1 - y0)
        x0, x1, y0, y1 = x0 - pad, x1 + pad, y0 - pad, y1 + pad
        k = res / max(x1 - x0, y1 - y0)
        gw, gh = int((x1 - x0) * k) + 1, int((y1 - y0) * k) + 1
        sil = Image.new("L", (gw, gh), 0)
        dr = ImageDraw.Draw(sil)
        gx, gy = (x - x0) * k, (y1 - y) * k
        for t in faces[:: max(1, len(faces) // 60000)]:
            dr.polygon([(gx[t[0]], gy[t[0]]), (gx[t[1]], gy[t[1]]), (gx[t[2]], gy[t[2]])], fill=255)
        S = np.array(sil) > 127
        yy, xx = np.mgrid[0:gh, 0:gw]
        X, Y = x0 + (xx + 0.5) / k, y1 - (yy + 0.5) / k
        A = self.alpha > 127

        def iou(sx, sy, tx, ty):
            px = (self.ic[0] + tx + (X - self.mc[0]) * sx).astype(int)
            py = (self.ic[1] + ty - (Y - self.mc[1]) * sy).astype(int)
            inside = (px >= 0) & (py >= 0) & (px < self.w) & (py < self.h)
            P = np.zeros_like(S)
            P[inside] = A[py[inside], px[inside]]
            inter = (P & S).sum()
            # Photo area that falls outside the grid also counts as a miss.
            union = (P | S).sum() + max(A.sum() / max(sx * sy / k ** 2, 1e-9) - P.sum(), 0)
            return inter / max(union, 1)

        best = (self.sx, self.sy, 0.0, 0.0)
        score = iou(*best)
        for step_s, step_t in ((0.12, 0.10), (0.05, 0.04), (0.02, 0.015), (0.008, 0.006)):
            improved = True
            while improved:
                improved = False
                sx, sy, tx, ty = best
                span = (self.w + self.h) / 2
                for cand in ((sx * (1 + step_s), sy, tx, ty), (sx * (1 - step_s), sy, tx, ty),
                             (sx, sy * (1 + step_s), tx, ty), (sx, sy * (1 - step_s), tx, ty),
                             (sx * (1 + step_s), sy * (1 + step_s), tx, ty), (sx * (1 - step_s), sy * (1 - step_s), tx, ty),
                             (sx, sy, tx + step_t * span, ty), (sx, sy, tx - step_t * span, ty),
                             (sx, sy, tx, ty + step_t * span), (sx, sy, tx, ty - step_t * span)):
                    v = iou(*cand)
                    if v > score + 1e-4:
                        score, best, improved = v, cand, True
        self.sx, self.sy = best[0], best[1]
        self.ic = self.ic + np.array([best[2], best[3]])
        return float(score)

    def project(self, pts: np.ndarray) -> np.ndarray:
        x, y = pts @ self.r, pts @ self.u
        px = self.ic[0] + (x - self.mc[0]) * self.sx
        py = self.ic[1] - (y - self.mc[1]) * self.sy
        return np.stack([px, py], -1)

    def sample(self, pix: np.ndarray):
        """Bilinear colour + alpha at float pixel coords."""
        x = np.clip(pix[:, 0], 0, self.w - 1.001)
        y = np.clip(pix[:, 1], 0, self.h - 1.001)
        x0, y0 = np.floor(x).astype(int), np.floor(y).astype(int)
        fx, fy = (x - x0)[:, None], (y - y0)[:, None]

        def bilinear(arr):
            a = arr if arr.ndim == 3 else arr[..., None]
            v = (a[y0, x0] * (1 - fx) * (1 - fy) + a[y0, x0 + 1] * fx * (1 - fy)
                 + a[y0 + 1, x0] * (1 - fx) * fy + a[y0 + 1, x0 + 1] * fx * fy)
            return v

        col = bilinear(self.rgb)
        a = bilinear(self.alpha.astype(np.float32))[:, 0]
        outside = (pix[:, 0] < 0) | (pix[:, 1] < 0) | (pix[:, 0] > self.w - 1) | (pix[:, 1] > self.h - 1)
        a[outside] = 0
        return col.astype(np.float32), a / 255.0


def _encode_ids(n: int) -> np.ndarray:
    ids = np.arange(1, n + 1, dtype=np.int64)
    return np.stack([(ids >> 16) & 255, (ids >> 8) & 255, ids & 255], -1)


def _decode_ids(img: np.ndarray) -> np.ndarray:
    img = img.astype(np.int64)
    return ((img[..., 0] << 16) | (img[..., 1] << 8) | img[..., 2]) - 1  # -1 = empty


def _draw_ids(tri_px: np.ndarray, order: np.ndarray, size: Tuple[int, int]) -> np.ndarray:
    """Rasterise triangles (already in pixel coords) into a face-ID image, in the given draw order."""
    img = Image.new("RGB", size, (0, 0, 0))
    draw = ImageDraw.Draw(img)
    colors = _encode_ids(len(tri_px))
    for i in order:
        t = tri_px[i]
        draw.polygon([(t[0, 0], t[0, 1]), (t[1, 0], t[1, 1]), (t[2, 0], t[2, 1])], fill=tuple(colors[i]))
    return _decode_ids(np.array(img))


def visibility(verts: np.ndarray, faces: np.ndarray, basis, pts: np.ndarray, res: int = 1024) -> np.ndarray:
    """True where `pts` (on the surface) are the front-most surface seen from this view."""
    r, u, f = basis
    x, y, z = verts @ r, verts @ u, verts @ f
    x0, x1, y0, y1 = x.min(), x.max(), y.min(), y.max()
    scale = (res - 4) / max(x1 - x0, y1 - y0, 1e-6)
    vx, vy = (x - x0) * scale + 2, (y1 - y) * scale + 2
    tri = np.stack([vx[faces], vy[faces]], -1)
    depth = z[faces].mean(1)
    ids = _draw_ids(tri, np.argsort(-depth), (res, res))  # far → near (painter's algorithm)
    px = np.clip(((pts @ r - x0) * scale + 2).astype(int), 0, res - 1)
    py = np.clip(((y1 - pts @ u) * scale + 2).astype(int), 0, res - 1)
    pz = pts @ f
    tol = 0.012 * max(x1 - x0, y1 - y0, z.max() - z.min())
    vis = np.zeros(len(pts), bool)
    for dx, dy in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)):  # tolerate rasterisation gaps
        hit = ids[np.clip(py + dy, 0, res - 1), np.clip(px + dx, 0, res - 1)]
        ok = hit >= 0
        front_depth = np.where(ok, depth[np.maximum(hit, 0)], np.inf)
        vis |= ok & (pz <= front_depth + tol)
    return vis


def unwrap(mesh: trimesh.Trimesh):
    vmap, faces, uvs = xatlas.parametrize(mesh.vertices.astype(np.float32), mesh.faces.astype(np.uint32))
    return vmap, faces.astype(np.int64), uvs.astype(np.float64)


def texel_samples(verts, normals, faces, uvs, size):
    """Rasterise the UV layout; return texel pixel coords, face ids, 3D points and normals."""
    tri_uv_px = np.stack([uvs[faces][..., 0] * size, (1 - uvs[faces][..., 1]) * size], -1)
    ids = _draw_ids(tri_uv_px, np.arange(len(faces)), (size, size))
    ty, tx = np.nonzero(ids >= 0)
    fid = ids[ty, tx]
    p = np.stack([tx + 0.5, ty + 0.5], -1)
    a, b, c = tri_uv_px[fid, 0], tri_uv_px[fid, 1], tri_uv_px[fid, 2]
    v0, v1, v2 = b - a, c - a, p - a
    d00, d01, d11 = (v0 * v0).sum(1), (v0 * v1).sum(1), (v1 * v1).sum(1)
    d20, d21 = (v2 * v0).sum(1), (v2 * v1).sum(1)
    den = d00 * d11 - d01 * d01
    den[np.abs(den) < 1e-12] = 1e-12
    w1 = (d11 * d20 - d01 * d21) / den
    w2 = (d00 * d21 - d01 * d20) / den
    bary = np.clip(np.stack([1 - w1 - w2, w1, w2], -1), 0, 1)
    bary /= bary.sum(1, keepdims=True)
    tri = faces[fid]
    pos = (verts[tri] * bary[..., None]).sum(1)
    nrm = (normals[tri] * bary[..., None]).sum(1)
    nrm /= np.linalg.norm(nrm, axis=1, keepdims=True) + 1e-9
    return tx, ty, fid, bary, pos, nrm


def _fibonacci_dirs(n: int) -> np.ndarray:
    i = np.arange(n) + 0.5
    phi = np.arccos(1 - 2 * i / n)
    theta = np.pi * (1 + 5 ** 0.5) * i
    return np.stack([np.cos(theta) * np.sin(phi), np.cos(phi), np.sin(theta) * np.sin(phi)], -1)


def _basis_for(d: np.ndarray):
    f = d / np.linalg.norm(d)
    up = np.array([0.0, 1.0, 0.0]) if abs(f[1]) < 0.95 else np.array([1.0, 0.0, 0.0])
    r = np.cross(f, up)
    r /= np.linalg.norm(r)
    return r, np.cross(r, f), f


def bake_ambient_occlusion(verts, normals, faces, uvs, size: int, n_dirs: int = 40, progress=None) -> np.ndarray:
    """Soft ambient occlusion in UV space: how much of the sky each surface point can see.
    Uses the same visibility test as the photo projection, from many directions around the model."""
    tx, ty, fid, bary, pos, nrm = texel_samples(verts, normals, faces, uvs, size)
    seen = np.zeros(len(pos), np.float32)
    total = np.zeros(len(pos), np.float32)
    for k, d in enumerate(_fibonacci_dirs(n_dirs)):
        if d[1] < -0.4:               # light rarely comes from under the floor
            continue
        basis = _basis_for(-d)        # camera looks from direction d toward the model
        cosw = np.clip(nrm @ d, 0, 1)
        m = cosw > 0.02
        if not m.any():
            continue
        vis = np.zeros(len(pos), bool)
        vis[m] = visibility(verts, faces, basis, pos[m], res=768)
        seen += cosw * vis
        total += cosw
        if progress:
            progress(k / n_dirs)
    ao = np.where(total > 0, seen / np.maximum(total, 1e-6), 1.0)
    img = np.ones((size, size), np.float32)
    img[ty, tx] = ao
    known = np.zeros((size, size), np.uint8)
    known[ty, tx] = 1
    # Pad islands so seams don't show, then soften (AO is a low-frequency effect).
    img8 = (np.clip(img, 0, 1) * 255).astype(np.uint8)
    img8 = cv2.inpaint(img8, (1 - known).astype(np.uint8), 3, cv2.INPAINT_TELEA)
    img8 = cv2.GaussianBlur(img8, (0, 0), max(1.0, size / 512))
    return img8.astype(np.float32) / 255.0


def project_texture(
    mesh: trimesh.Trimesh,
    views: Dict[str, Image.Image],
    front_forward=(0, 0, -1),
    yaw_sign: float = 1.0,
    size: int = 2048,
    fallback_vertex_colors: Optional[np.ndarray] = None,
    progress=None,
    bake_ao: bool = True,
) -> trimesh.Trimesh:
    """Return a UV-textured copy of `mesh` painted from `views` ({'front': RGBA image, ...})."""
    def report(p, msg):
        if progress:
            progress(p, msg)

    report(0.0, "Unwrapping UVs…")
    vmap, faces, uvs = unwrap(mesh)
    verts = mesh.vertices[vmap]
    normals = mesh.vertex_normals[vmap]
    vcol = fallback_vertex_colors[vmap][:, :3].astype(np.float32) if fallback_vertex_colors is not None else None

    report(0.25, "Painting texture from your photos…")
    tx, ty, fid, bary, pos, nrm = texel_samples(verts, normals, faces, uvs, size)
    # Head band: the top ~17% of the model. The face there should come from the front photo only.
    y_axis = np.array([0.0, 1.0, 0.0])
    hy = pos @ y_axis
    head = hy > hy.max() - 0.17 * (hy.max() - hy.min())

    per_view = {}
    for i, (name, img) in enumerate(views.items()):
        if img is None or name not in VIEW_YAW:
            continue
        basis = view_basis(np.asarray(front_forward, float), VIEW_YAW[name], yaw_sign)
        proj = ViewProjector(verts, img, basis, faces=faces)
        print(f"[Texture] {name} view aligned (outline overlap {proj.iou:.0%})")
        cosang = nrm @ -basis[2]
        # Ignore steep, grazing surfaces (>~70° from the camera): they only smear stretched pixels.
        facing = np.clip((cosang - 0.3) / 0.7, 0, 1)
        vis = visibility(verts, faces, basis, pos, res=max(1024, min(size // 2, 2048)))
        pix = proj.project(pos)
        col, a = proj.sample(pix)
        # Fade out the last few pixels inside the cut-out edge: they often carry background halo.
        inside = (proj.alpha > 127).astype(np.uint8)
        dist = cv2.distanceTransform(inside, cv2.DIST_L2, 5)
        px = np.clip(pix[:, 0].astype(int), 0, proj.w - 1)
        py = np.clip(pix[:, 1].astype(int), 0, proj.h - 1)
        edge = np.clip(dist[py, px] / max(4.0, 0.008 * max(proj.w, proj.h)), 0, 1)
        # Sharp falloff so each surface takes colour mostly from the photo facing it (less ghosting).
        w = (facing ** 6) * vis * (a > 0.5) * edge
        if name == "front":
            w = w * 1.5
            w[head & (cosang > 0.35)] *= 8.0        # face priority
        else:
            w[head & ((nrm @ -view_basis(np.asarray(front_forward, float), 0, yaw_sign)[2]) > 0.55)] *= 0.05
        per_view[name] = (col.astype(np.float32), w.astype(np.float32))
        report(0.25 + 0.5 * (i + 1) / max(len(views), 1), "Painting texture from your photos…")

    # Match every photo's colour/exposure to the front one where they overlap, so seams disappear.
    if "front" in per_view:
        fcol, fw = per_view["front"]
        for name, (col, w) in per_view.items():
            if name == "front":
                continue
            ov = (fw > 0.02) & (w > 0.02)
            if ov.sum() < 500:
                continue
            gain = np.median(fcol[ov], axis=0) / np.maximum(np.median(col[ov], axis=0), 1.0)
            gain = np.clip(gain, 0.75, 1.33)
            per_view[name] = (np.clip(col * gain, 0, 255), w)
            print(f"[Texture] {name} colour matched to front (gain {np.round(gain, 2).tolist()})")

    acc = np.zeros((len(pos), 3), np.float32)
    wsum = np.zeros(len(pos), np.float32)
    for col, w in per_view.values():
        acc += col * w[:, None]
        wsum += w
    del per_view

    report(0.8, "Filling hidden areas…")
    seen = wsum > 1e-3
    colors = np.zeros_like(acc)
    colors[seen] = acc[seen] / wsum[seen, None]
    unseen = ~seen
    if unseen.any():
        if vcol is not None:  # optional per-vertex colours are a good guess for hidden sides
            colors[unseen] = (vcol[faces[fid[unseen]]] * bary[unseen][..., None]).sum(1)
            seen[:] = True
        elif seen.any():
            # Continue the nearest painted surface (in 3D) around the hidden sides and back:
            # smooth and plausible, and no "second face" painted on the back of a head.
            from scipy.spatial import cKDTree
            src = np.nonzero(seen)[0]
            pick = src[:: max(1, len(src) // 400000)]
            tree = cKDTree(pos[pick])
            dist, nn = tree.query(pos[unseen], k=8)
            w = 1.0 / (dist + 1e-4)
            colors[unseen] = (colors[pick][nn] * w[..., None]).sum(1) / w.sum(1, keepdims=True)
            seen[:] = True

    tex = np.zeros((size, size, 3), np.uint8)
    mask = np.ones((size, size), np.uint8) * 255
    tex[ty[seen], tx[seen]] = np.clip(colors[seen], 0, 255).astype(np.uint8)
    mask[ty[seen], tx[seen]] = 0
    # Inpaint holes and pad every UV island outward so seams don't show dark lines.
    small = cv2.resize(tex, (size // 4, size // 4), interpolation=cv2.INTER_AREA)
    small_mask = cv2.resize(mask, (size // 4, size // 4), interpolation=cv2.INTER_NEAREST)
    small = cv2.inpaint(small, small_mask, 3, cv2.INPAINT_TELEA)
    filled = cv2.resize(small, (size, size), interpolation=cv2.INTER_LINEAR)
    tex = np.where(mask[..., None] > 0, filled, tex)
    # Gentle sharpening brings back fine print/skin detail lost in resampling.
    blur = cv2.GaussianBlur(tex, (0, 0), 1.6)
    tex = cv2.addWeighted(tex, 1.45, blur, -0.45, 0)
    report(0.95, "Saving textured model…")

    occlusion = None
    if bake_ao:
        report(0.9, "Baking soft shadows (ambient occlusion)…")
        ao_size = max(512, size // 4)
        ao = bake_ambient_occlusion(verts, normals, faces, uvs, ao_size,
                                    progress=lambda p: report(0.9 + 0.05 * p, "Baking soft shadows (ambient occlusion)…"))
        ao_full = cv2.resize(ao, (size, size), interpolation=cv2.INTER_LINEAR)
        # Burn a gentle amount into the colour (for viewers/exports without AO support)…
        tex = np.clip(tex.astype(np.float32) * (0.72 + 0.28 * ao_full[..., None]), 0, 255).astype(np.uint8)
        # …and ship the full AO map as glTF occlusion (red channel) for Blender / three.js / model-viewer.
        ao8 = (np.clip(ao_full, 0, 1) * 255).astype(np.uint8)
        occlusion = Image.fromarray(np.stack([ao8, ao8, ao8], -1))
        print(f"[Texture] ambient occlusion baked (mean {ao.mean():.2f})")

    material = trimesh.visual.material.PBRMaterial(
        baseColorTexture=Image.fromarray(tex), metallicFactor=0.0, roughnessFactor=0.82, doubleSided=False,
        occlusionTexture=occlusion,
    )
    visual = trimesh.visual.TextureVisuals(uv=uvs, material=material)
    return trimesh.Trimesh(vertices=verts, faces=faces, visual=visual, process=False)
