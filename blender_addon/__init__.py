bl_info = {
    "name": "WebSocket Auto Import",
    "author": "ahujasid",
    "version": (0, 2, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > WS Import",
    "description": "Connects to the Camera to 3D relay server and auto-imports scans, "
                   "with smart placement, history and a gallery browser",
    "category": "Import-Export",
}

import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.request
from urllib.parse import urljoin, urlparse

import bpy
from mathutils import Vector


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def have_websocket() -> bool:
    try:
        import websocket  # noqa: F401
        return True
    except ImportError:
        return False


def http_base(ws_url: str) -> str:
    """ws://host:8000/ws?client=blender -> http://host:8000/"""
    p = urlparse(ws_url)
    scheme = "https" if p.scheme == "wss" else "http"
    return f"{scheme}://{p.netloc}/"


def resolve_url(ws_url: str, url: str) -> str:
    return url if urlparse(url).scheme in ("http", "https") else urljoin(http_base(ws_url), url.lstrip("/"))


def download_file(url: str) -> str:
    suffix = ".obj" if urlparse(url).path.lower().endswith(".obj") else ".glb"
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    req = urllib.request.Request(url, headers={"User-Agent": "camera-to-blender-addon"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(path, "wb") as f:
        f.write(resp.read())
    return path


def settings(context=None):
    return (context or bpy.context).window_manager.ws_import


def tag_redraw():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


# ─────────────────────────────────────────────────────────────────────────────
# Smart import
# ─────────────────────────────────────────────────────────────────────────────
def import_model(url: str, name: str = "scan") -> bpy.types.Object:
    s = settings()
    path = download_file(url)
    before = set(bpy.data.objects)
    try:
        if path.endswith(".obj"):
            bpy.ops.wm.obj_import(filepath=path)
        else:
            bpy.ops.import_scene.gltf(filepath=path)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    new = [o for o in bpy.data.objects if o not in before]
    if not new:
        raise RuntimeError("Importer created no objects")
    roots = [o for o in new if o.parent not in new]

    # Group everything under one empty so it moves/scales as one object.
    parent = bpy.data.objects.new(name, None)
    parent.empty_display_type = "PLAIN_AXES"
    parent.empty_display_size = 0.25
    collection = roots[0].users_collection[0] if roots[0].users_collection else bpy.context.scene.collection
    collection.objects.link(parent)
    for r in roots:
        r.parent = parent
        r.matrix_parent_inverse.identity()
    bpy.context.view_layer.update()

    meshes = [o for o in new if o.type == "MESH"]
    corners = [o.matrix_world @ Vector(c) for o in meshes for c in o.bound_box]
    if corners:
        lo = Vector((min(c.x for c in corners), min(c.y for c in corners), min(c.z for c in corners)))
        hi = Vector((max(c.x for c in corners), max(c.y for c in corners), max(c.z for c in corners)))
        size = max(hi - lo)
        scale = s.target_size / size if (s.normalize_size and size > 1e-6) else 1.0
        parent.scale = (scale, scale, scale)
        target = bpy.context.scene.cursor.location.copy() if s.place_at_cursor else Vector((0, 0, 0))
        center = (lo + hi) / 2
        anchor = Vector((center.x, center.y, lo.z if s.drop_to_ground else center.z))
        parent.location = target - anchor * scale

    if s.decimate_ratio < 0.999:
        for o in meshes:
            mod = o.modifiers.new("Decimate", "DECIMATE")
            mod.ratio = s.decimate_ratio

    if s.shade_smooth:
        for o in meshes:
            for poly in o.data.polygons:
                poly.use_smooth = True

    # Fit the timeline to any imported animation.
    actions = [o.animation_data.action for o in new if o.animation_data and o.animation_data.action]
    if actions:
        start = min(int(a.frame_range[0]) for a in actions)
        end = max(int(a.frame_range[1]) for a in actions)
        bpy.context.scene.frame_start, bpy.context.scene.frame_end = start, max(end, start + 1)

    if s.add_floor and corners:
        floor_name = "ScanFloor"
        if floor_name not in bpy.data.objects:
            bpy.ops.mesh.primitive_plane_add(size=max(s.target_size * 6, 2), location=parent.location)
            floor = bpy.context.active_object
            floor.name = floor_name
            if hasattr(floor, "is_shadow_catcher"):
                floor.is_shadow_catcher = True  # Cycles: shows only the shadow

    for o in bpy.context.selected_objects:
        o.select_set(False)
    parent.select_set(True)
    bpy.context.view_layer.objects.active = parent
    return parent


def add_history(name: str, url: str):
    s = settings()
    for item in s.history:
        if item.url == url:
            return
    item = s.history.add()
    item.name, item.url = name, url
    s.history.move(len(s.history) - 1, 0)
    while len(s.history) > 30:
        s.history.remove(len(s.history) - 1)


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket connection (background thread → queue → main-thread timer)
# ─────────────────────────────────────────────────────────────────────────────
class WSImportState:
    def __init__(self):
        self.ws_thread = None
        self.ws_app = None
        self.ws_url = ""
        self.connected = False
        self.wanted = False
        self.queue = []
        self.last_error = ""
        self.last_import = ""

    def start(self, ws_url: str):
        from websocket import WebSocketApp

        if self.ws_thread and self.ws_thread.is_alive():
            return
        self.ws_url, self.wanted, self.last_error = ws_url, True, ""

        def on_message(_, message):
            try:
                data = json.loads(message)
            except Exception:
                return
            if data.get("type") == "model" and data.get("url"):
                self.queue.append(data)

        def on_open(_):
            self.connected, self.last_error = True, ""

        def on_close(*_):
            self.connected = False

        def on_error(_, err):
            self.last_error = str(err)[:120]

        def run():
            while self.wanted:
                self.ws_app = WebSocketApp(ws_url, on_message=on_message, on_open=on_open,
                                           on_close=on_close, on_error=on_error)
                self.ws_app.run_forever(ping_interval=20, ping_timeout=10)
                self.connected = False
                if self.wanted:
                    threading.Event().wait(3)  # auto-reconnect after 3s

        self.ws_thread = threading.Thread(target=run, daemon=True)
        self.ws_thread.start()
        if not bpy.app.timers.is_registered(_tick):
            bpy.app.timers.register(_tick, first_interval=0.5, persistent=True)

    def stop(self):
        self.wanted = False
        if self.ws_app:
            self.ws_app.close()
        self.connected = False
        self.ws_app = None
        self.ws_thread = None
        self.queue.clear()

    def _tick(self):
        if not self.queue:
            tag_redraw()
            return 1.0
        data = self.queue.pop(0)
        url = resolve_url(self.ws_url, data["url"])
        name = data.get("name") or "scan"
        try:
            obj = import_model(url, name)
            add_history(obj.name, url)
            self.last_import = obj.name
            print(f"[WSImport] ✓ Imported {obj.name}")
        except Exception as e:
            self.last_error = f"Import failed: {e}"[:120]
            print(f"[WSImport] ✗ Failed to import {url[:80]}: {e}")
        tag_redraw()
        return 0.1


state = WSImportState()


def _tick():
    return state._tick()


# ─────────────────────────────────────────────────────────────────────────────
# Properties
# ─────────────────────────────────────────────────────────────────────────────
class WSHistoryItem(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty()
    url: bpy.props.StringProperty()
    thumb: bpy.props.StringProperty()
    kind: bpy.props.StringProperty()


class WSImportSettings(bpy.types.PropertyGroup):
    place_at_cursor: bpy.props.BoolProperty(name="Place at 3D Cursor", default=True)
    drop_to_ground: bpy.props.BoolProperty(name="Sit on Ground", default=True,
                                           description="Put the bottom of the model at the cursor height")
    normalize_size: bpy.props.BoolProperty(name="Normalize Size", default=True)
    target_size: bpy.props.FloatProperty(name="Size", default=1.0, min=0.01, soft_max=10.0, unit="LENGTH",
                                         description="Largest dimension after import")
    decimate_ratio: bpy.props.FloatProperty(name="Keep Polys", default=1.0, min=0.02, max=1.0, subtype="FACTOR",
                                            description="Add a Decimate modifier below 1.0 to lighten heavy scans")
    shade_smooth: bpy.props.BoolProperty(name="Shade Smooth", default=False)
    add_floor: bpy.props.BoolProperty(name="Add Shadow Floor", default=False)
    history: bpy.props.CollectionProperty(type=WSHistoryItem)
    history_index: bpy.props.IntProperty()
    gallery: bpy.props.CollectionProperty(type=WSHistoryItem)
    gallery_index: bpy.props.IntProperty()


class WSImportPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    ws_url: bpy.props.StringProperty(
        name="WebSocket URL",
        default="ws://localhost:8000/ws?client=blender",
        description="ws://localhost:8000/ws?client=blender for local; wss://<ngrok>/ws?client=blender for ngrok",
    )
    auto_connect: bpy.props.BoolProperty(name="Connect on Startup", default=False)

    def draw(self, context):
        self.layout.prop(self, "ws_url")
        self.layout.prop(self, "auto_connect")


def prefs(context=None):
    return (context or bpy.context).preferences.addons[__name__].preferences


# ─────────────────────────────────────────────────────────────────────────────
# Operators
# ─────────────────────────────────────────────────────────────────────────────
class WSImportOperator(bpy.types.Operator):
    bl_idname = "wm.ws_auto_import_toggle"
    bl_label = "Connect / Disconnect"
    bl_description = "Connect/disconnect the WebSocket auto importer"

    def execute(self, context):
        if state.wanted:
            state.stop()
            self.report({"INFO"}, "WebSocket import stopped")
        else:
            if not have_websocket():
                self.report({"ERROR"}, "Install the websocket-client dependency first")
                return {"CANCELLED"}
            state.start(prefs(context).ws_url)
            self.report({"INFO"}, f"Connecting to {prefs(context).ws_url}")
        return {"FINISHED"}


class WSInstallDependency(bpy.types.Operator):
    bl_idname = "wm.ws_install_dependency"
    bl_label = "Install websocket-client"
    bl_description = "Install the websocket-client package into Blender's Python (needs internet)"

    def execute(self, context):
        try:
            subprocess.run([sys.executable, "-m", "ensurepip", "--user"], capture_output=True)
            r = subprocess.run([sys.executable, "-m", "pip", "install", "--user", "websocket-client"],
                               capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(r.stderr[-300:])
            import site
            import importlib
            user_site = site.getusersitepackages()
            if user_site not in sys.path:
                sys.path.append(user_site)
            importlib.invalidate_caches()
        except Exception as e:
            self.report({"ERROR"}, f"Install failed: {e}")
            return {"CANCELLED"}
        self.report({"INFO"}, "websocket-client installed")
        return {"FINISHED"}


class WSImportURL(bpy.types.Operator):
    bl_idname = "wm.ws_import_url"
    bl_label = "Import"
    bl_description = "Import this model again with the current import settings"

    url: bpy.props.StringProperty()
    name: bpy.props.StringProperty(default="scan")

    def execute(self, context):
        try:
            obj = import_model(resolve_url(prefs(context).ws_url, self.url), self.name)
            add_history(obj.name, resolve_url(prefs(context).ws_url, self.url))
        except Exception as e:
            self.report({"ERROR"}, f"Import failed: {e}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Imported {obj.name}")
        return {"FINISHED"}


class WSFetchGallery(bpy.types.Operator):
    bl_idname = "wm.ws_fetch_gallery"
    bl_label = "Refresh Gallery"
    bl_description = "List every scan saved by the relay server"

    def execute(self, context):
        base = http_base(prefs(context).ws_url)
        try:
            req = urllib.request.Request(urljoin(base, "scans"), headers={"User-Agent": "camera-to-blender-addon"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                scans = json.loads(resp.read()).get("scans", [])
        except Exception as e:
            self.report({"ERROR"}, f"Could not reach server at {base}: {e}")
            return {"CANCELLED"}
        s = settings(context)
        s.gallery.clear()
        for scan in scans:
            files = scan.get("file_urls") or {}
            for role, url in files.items():
                if role == "preview":
                    continue
                item = s.gallery.add()
                label = scan.get("prompt") or scan.get("name") or scan["id"]
                item.name = label if role == "final" else f"{label} · {role.replace('anim_', 'anim: ')}"
                item.url = url
                item.kind = scan.get("kind", "")
        self.report({"INFO"}, f"{len(s.gallery)} models on the server")
        return {"FINISHED"}


class WSClearHistory(bpy.types.Operator):
    bl_idname = "wm.ws_clear_history"
    bl_label = "Clear History"

    def execute(self, context):
        settings(context).history.clear()
        return {"FINISHED"}


# ─────────────────────────────────────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────────────────────────────────────
class WS_UL_models(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.label(text=item.name, icon="OUTLINER_OB_MESH")
        op = row.operator(WSImportURL.bl_idname, text="", icon="IMPORT")
        op.url, op.name = item.url, item.name.split(" · ")[0][:40] or "scan"


class WSImportPanel(bpy.types.Panel):
    bl_label = "Camera → 3D"
    bl_idname = "VIEW3D_PT_ws_auto_import"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "WS Import"

    def draw(self, context):
        layout = self.layout
        p = prefs(context)
        if not have_websocket():
            box = layout.box()
            box.label(text="Missing websocket-client", icon="ERROR")
            box.operator(WSInstallDependency.bl_idname, icon="IMPORT")
            return
        layout.prop(p, "ws_url", text="")
        row = layout.row()
        row.scale_y = 1.3
        row.operator(WSImportOperator.bl_idname, text="Disconnect" if state.wanted else "Connect",
                     icon="UNLINKED" if state.wanted else "LINKED")
        if state.connected:
            layout.label(text="Status: Connected", icon="CHECKMARK")
        elif state.wanted:
            layout.label(text="Status: Connecting…", icon="TIME")
        else:
            layout.label(text="Status: Disconnected", icon="UNLINKED")
        if state.last_error:
            layout.label(text=state.last_error, icon="ERROR")
        if state.last_import:
            layout.label(text=f"Last: {state.last_import}", icon="IMPORT")


class WSImportOptionsPanel(bpy.types.Panel):
    bl_label = "Import Options"
    bl_idname = "VIEW3D_PT_ws_import_options"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "WS Import"
    bl_parent_id = WSImportPanel.bl_idname

    def draw(self, context):
        s = settings(context)
        col = self.layout.column(align=True)
        col.prop(s, "place_at_cursor")
        col.prop(s, "drop_to_ground")
        row = col.row(align=True)
        row.prop(s, "normalize_size")
        sub = row.row(align=True)
        sub.active = s.normalize_size
        sub.prop(s, "target_size", text="")
        col.prop(s, "decimate_ratio")
        col.prop(s, "shade_smooth")
        col.prop(s, "add_floor")


class WSHistoryPanel(bpy.types.Panel):
    bl_label = "History"
    bl_idname = "VIEW3D_PT_ws_import_history"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "WS Import"
    bl_parent_id = WSImportPanel.bl_idname
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        s = settings(context)
        if not s.history:
            self.layout.label(text="Imported scans appear here")
            return
        self.layout.template_list("WS_UL_models", "history", s, "history", s, "history_index", rows=4)
        self.layout.operator(WSClearHistory.bl_idname, icon="TRASH")


class WSGalleryPanel(bpy.types.Panel):
    bl_label = "Server Gallery"
    bl_idname = "VIEW3D_PT_ws_import_gallery"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "WS Import"
    bl_parent_id = WSImportPanel.bl_idname
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        s = settings(context)
        self.layout.operator(WSFetchGallery.bl_idname, icon="FILE_REFRESH")
        if s.gallery:
            self.layout.template_list("WS_UL_models", "gallery", s, "gallery", s, "gallery_index", rows=5)


classes = (
    WSHistoryItem, WSImportSettings, WSImportPreferences,
    WSImportOperator, WSInstallDependency, WSImportURL, WSFetchGallery, WSClearHistory,
    WS_UL_models, WSImportPanel, WSImportOptionsPanel, WSHistoryPanel, WSGalleryPanel,
)


def _auto_connect():
    try:
        if prefs().auto_connect and have_websocket():
            state.start(prefs().ws_url)
    except Exception as e:
        print(f"[WSImport] Auto-connect failed: {e}")
    return None


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.ws_import = bpy.props.PointerProperty(type=WSImportSettings)
    bpy.app.timers.register(_auto_connect, first_interval=2.0)


def unregister():
    state.stop()
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    del bpy.types.WindowManager.ws_import
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
