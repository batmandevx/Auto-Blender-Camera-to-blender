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

    def __init__(self, verts: np.ndarray, image: Image.Image, basis):
        self.r, self.u, self.f = basis
        rgba = np.array(image.convert("RGBA"))
        self.rgb = rgba[..., :3].astype(np.float32)
        self.alpha = rgba[..., 3]
        self.h, self.w = self.alpha.shape
        x, y = verts @ self.r, verts @ self.u
        mx0, mx1, my0, my1 = x.min(), x.max(), y.min(), y.max()
        bb = _alpha_bbox(self.alpha) or (0, 0, self.w - 1, self.h - 1)
        iw, ih = bb[2] - bb[0], bb[3] - bb[1]
        mw, mh = max(mx1 - mx0, 1e-6), max(my1 - my0, 1e-6)
        self.s = 0.5 * (iw / mw + ih / mh)
        self.mc = np.array([(mx0 + mx1) / 2, (my0 + my1) / 2])
        self.ic = np.array([(bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2])

    def project(self, pts: np.ndarray) -> np.ndarray:
        x, y = pts @ self.r, pts @ self.u
        px = self.ic[0] + (x - self.mc[0]) * self.s
        py = self.ic[1] - (y - self.mc[1]) * self.s
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


def project_texture(
    mesh: trimesh.Trimesh,
    views: Dict[str, Image.Image],
    front_forward=(0, 0, -1),
    yaw_sign: float = 1.0,
    size: int = 2048,
    fallback_vertex_colors: Optional[np.ndarray] = None,
    progress=None,
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
    acc = np.zeros((len(pos), 3), np.float32)
    wsum = np.zeros(len(pos), np.float32)

    front_proj = None
    for i, (name, img) in enumerate(views.items()):
        if img is None or name not in VIEW_YAW:
            continue
        basis = view_basis(np.asarray(front_forward, float), VIEW_YAW[name], yaw_sign)
        proj = ViewProjector(verts, img, basis)
        if name == "front":
            front_proj = (proj, basis)
        facing = np.clip(nrm @ -basis[2], 0, 1)
        vis = visibility(verts, faces, basis, pos, res=max(1024, min(size // 2, 2048)))
        col, a = proj.sample(proj.project(pos))
        # Sharp falloff so each surface takes colour mostly from the photo facing it (less ghosting);
        # the front photo is usually the sharpest, so it gets a little extra say.
        w = (facing ** 6) * vis * (a > 0.5) * (1.5 if name == "front" else 1.0)
        acc += col * w[:, None]
        wsum += w
        report(0.25 + 0.5 * (i + 1) / max(len(views), 1), "Painting texture from your photos…")

    report(0.8, "Filling hidden areas…")
    seen = wsum > 1e-3
    colors = np.zeros_like(acc)
    colors[seen] = acc[seen] / wsum[seen, None]
    unseen = ~seen
    if unseen.any():
        if vcol is not None:  # optional per-vertex colours are a good guess for hidden sides
            colors[unseen] = (vcol[faces[fid[unseen]]] * bary[unseen][..., None]).sum(1)
            seen[:] = True
        elif front_proj is not None:  # see-through projection of the front photo onto the back
            proj, basis = front_proj
            col, a = proj.sample(proj.project(pos[unseen]))
            ok = a > 0.5
            idx = np.nonzero(unseen)[0][ok]
            colors[idx] = col[ok]
            seen[idx] = True

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
    report(0.95, "Saving textured model…")

    material = trimesh.visual.material.PBRMaterial(
        baseColorTexture=Image.fromarray(tex), metallicFactor=0.0, roughnessFactor=0.75, doubleSided=False,
    )
    visual = trimesh.visual.TextureVisuals(uv=uvs, material=material)
    return trimesh.Trimesh(vertices=verts, faces=faces, visual=visual, process=False)
