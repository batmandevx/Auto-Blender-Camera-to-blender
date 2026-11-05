<div align="center">

<img src="webapp/icon-192.png" width="96" alt="Camera to 3D logo">

# Camera to 3D → Blender

**Point your phone at an object, take a photo, and get a textured 3D model you can animate in Blender, place in your room with AR, or 3D-print.**

Everything runs **on your own Mac**. There are no API keys, no cloud, no credits, and your photos never leave your computer.

![Platform](https://img.shields.io/badge/platform-macOS%20(Apple%20Silicon)-black?logo=apple)
![Python](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)
![Blender](https://img.shields.io/badge/Blender-3.0%2B-F5792A?logo=blender&logoColor=white)
![3D engine](https://img.shields.io/badge/3D%20engine-Hunyuan3D--2-6f42c1)
![License](https://img.shields.io/badge/license-MIT-green)

<img src="docs/images/example-gun-4view.jpg" width="820" alt="Four photos of a toy blaster turned into a textured 3D model">
<img width="1280" height="256" alt="image" src="https://github.com/user-attachments/assets/952dcb99-142f-4733-aee1-50fbd7814c8d" />

</div>

---

## Table of contents

1. [What it does](#what-it-does)
2. [Screenshots](#screenshots)
3. [How it works (architecture)](#how-it-works-architecture)
4. [What you need](#what-you-need)
5. [Install on your computer (step by step)](#install-on-your-computer-step-by-step)
6. [Start the app](#start-the-app)
7. [Use it on your smartphone](#use-it-on-your-smartphone)
8. [Make your first 3D model](#make-your-first-3d-model)
9. [Send the model to Blender](#send-the-model-to-blender)
10. [Animate it in Blender](#animate-it-in-blender)
11. [Play, AR and talking characters](#play-ar-and-talking-characters)
12. [Edit and export](#edit-and-export)
13. [Quality presets and timing](#quality-presets-and-timing)
14. [Tips for great scans](#tips-for-great-scans)
15. [Troubleshooting](#troubleshooting)
16. [Settings (`.env`)](#settings-env)
17. [API reference](#api-reference)
18. [Project structure](#project-structure)
19. [Licenses](#license)

---

## What it does

```mermaid
flowchart LR
    A["📱 Take a photo<br/>(phone or laptop)"] --> B["👆 Tap the object<br/>to cut it out"]
    B --> C["🧊 3D shape<br/>Hunyuan3D-2"]
    C --> D["🎨 Paint your photo<br/>onto the model"]
    D --> E["🟧 Blender<br/>one click"]
    D --> F["🏠 AR in your room"]
    D --> G["🖨️ STL / OBJ / GLB / USDZ"]
    D --> H["💬 Talk to it<br/>(local AI)"]
```

| | Feature | Details |
| --- | --- | --- |
| 📸 | **Photo or 4-VIEW capture** | One photo is enough. Shoot front, left, back and right for a model with a real back instead of a guessed one. |
| 👆 | **Tap-to-select cut-out** | Tap the object and SAM cuts out exactly that thing, even with hands or clutter around it. You can also use **− Remove** or **✨ Auto**. |
| 🧊 | **Local 3D generation** | [Hunyuan3D-2](https://github.com/Tencent-Hunyuan/Hunyuan3D-2) runs on your Mac's GPU (Apple MPS). |
| 🎨 | **Photo-projection textures** | Your photos are painted onto a real UV texture (1K, 2K or 4K) instead of blurry vertex colours. |
| 🧹 | **Clean meshes** | Floating blobs and broken faces are removed, and the model is centred, set on the ground and facing forward. |
| 🟧 | **Send to Blender** | The Blender add-on imports the model as soon as you press **Send to Blender**. You can include a baked animation and pick a real-world size. |
| 🕺 | **18 baked animations** | Wave, dance, walk, run, backflip, spin, swim, sleep and more, ready to play in Blender's timeline. |
| 🏠 | **AR** | Android Room AR (WebXR), iPhone AR Quick Look (USDZ), and ✋ hand tracking on any camera. |
| 💬 | **Talking characters** | Gemma 3 identifies the object, Whisper hears you and Kokoro speaks back. All three run locally. |
| 📦 | **Export** | GLB, OBJ + texture (zip), STL (100 mm, for printing), PLY, USDZ, animated GLB. |
| 🗂️ | **Gallery** | Every scan is saved on your Mac with a thumbnail, and you can re-open, rename, refine or vary it. |

---

## Screenshots

### Laptop

| Start screen | Your scans (gallery) |
| --- | --- |
| ![Start screen](docs/images/desktop-home.jpg) | ![Gallery](docs/images/desktop-gallery.jpg) |
| **A finished model** | **Model tools** |
| ![Finished model](docs/images/desktop-model.jpg) | ![Model tools](docs/images/desktop-ready.jpg) |
| **Blender tab: connect and send** | **Export tab** |
| ![Blender tab](docs/images/desktop-tab-blender.jpg) | ![Export tab](docs/images/desktop-tab-export.jpg) |
| **Edit tab** | **Play: gun mode with targets** |
| ![Edit tab](docs/images/desktop-tab-edit.jpg) | ![Gun play mode](docs/images/play-gun.jpg) |

**Play: character mode (animations and voice chat)**

![Character play mode](docs/images/play-character.jpg)

### Smartphone

| Camera | Gallery | Model | Tools | Blender | Play & talk |
| --- | --- | --- | --- | --- | --- |
| ![](docs/images/mobile-home.jpg) | ![](docs/images/mobile-gallery.jpg) | ![](docs/images/mobile-model.jpg) | ![](docs/images/mobile-ready.jpg) | ![](docs/images/mobile-tab-blender.jpg) | ![](docs/images/mobile-play.jpg) |

### Example results

| One photo → model | Four photos → model |
| --- | --- |
| ![Doll example](docs/images/example-doll.jpg) | ![Blaster example](docs/images/example-gun-4view.jpg) |

---

## How it works (architecture)

### The big picture

The Mac runs **one Python server**, which listens on two ports. The **phone** connects over HTTPS (browsers only allow camera access on secure pages), the **laptop browser** uses plain HTTP, and **Blender** stays connected through a WebSocket so models can be pushed to it as soon as they are ready.

```mermaid
flowchart TB
    subgraph Devices["Your devices"]
        Phone["📱 Smartphone<br/>Safari / Chrome"]
        Laptop["💻 Laptop browser"]
        Blender["🟧 Blender<br/>WS Import add-on"]
    end

    subgraph Mac["Your Mac (serve.py, one process)"]
        direction TB
        HTTPS[":8443 HTTPS<br/>self-signed cert"]
        HTTP[":8000 HTTP"]
        API["relay_server.py<br/>FastAPI + WebSocket hub"]

        subgraph Pipeline["3D pipeline (local3d.py)"]
            Cut["Cut-out<br/>rembg / SAM"]
            Shape["Shape<br/>Hunyuan3D-2 on MPS"]
            Clean["Cleanup<br/>floaters, faces, budget"]
            Tex["Texture<br/>texture_project.py<br/>xatlas UV + photo projection"]
        end

        AR["ar_export.py<br/>animated GLB + USDZ"]
        AI["ai_local.py<br/>Gemma 3 · Whisper · Kokoro"]
        Store[("scans/<br/>GLB, photos, meta.json")]
        Ollama["Ollama<br/>gemma3:4b"]
    end

    Phone -- "camera, photos" --> HTTPS
    Laptop --> HTTP
    Blender <-- "ws://…/ws?client=blender" --> HTTP
    HTTPS --> API
    HTTP --> API
    API --> Cut --> Shape --> Clean --> Tex --> Store
    API --> AR --> Store
    API --> AI --> Ollama
    API -- "push model URL" --> Blender
```

### What happens when you press the shutter

```mermaid
sequenceDiagram
    autonumber
    actor You
    participant App as Web app (phone/laptop)
    participant S as relay_server.py
    participant P as local3d.py (worker thread)
    participant B as Blender add-on

    You->>App: Take photo, tap the object
    App->>S: POST /segment (tap points)
    S-->>App: Cut-out PNG (SAM)
    You->>App: Generate
    App->>S: POST /generate3d (photo/s, quality)
    S->>P: Queue job
    S-->>App: job id
    App->>S: GET /jobs/{id}/stream (live progress)
    P->>P: Load model → sculpt shape (~20–55%)
    P->>P: Cleanup mesh (55%)
    P-->>App: event: preview (untextured GLB)
    P->>P: UV unwrap + paint texture (60–98%)
    P-->>App: event: complete (final.glb)
    You->>App: Send to Blender
    App->>S: POST /relay (model URL, animation, size)
    S->>B: WebSocket message
    B->>S: Download GLB
    B->>B: Import, place, fit timeline
```

### The generation pipeline in detail

```mermaid
flowchart LR
    subgraph Input
        P1["Photo(s)<br/>front / left / back / right"]
    end
    subgraph CutOut["1 · Cut-out"]
        SAM["SAM tap-to-select"]
        REM["rembg auto<br/>isnet-general-use"]
    end
    subgraph Shape["2 · Shape (Hunyuan3D-2)"]
        DIT["Diffusion transformer<br/>single / multi-view / full"]
        VAE["VAE volume decode<br/>octree 256–448"]
        MC["Marching cubes → mesh"]
    end
    subgraph Cleanup["3 · Cleanup"]
        DET["Drop detached blobs"]
        FL["Floater remover"]
        DG["Degenerate faces"]
        FR["Reduce to face budget<br/>20k–250k"]
        NM["Normalize: centre, floor, scale"]
    end
    subgraph Texture["4 · Texture"]
        UV["xatlas UV unwrap"]
        PR["Project each photo<br/>visibility + facing weights"]
        BL["Blend views → 1K/2K/4K PNG"]
    end
    OUT[("final.glb")]

    P1 --> SAM & REM --> DIT --> VAE --> MC --> DET --> FL --> DG --> FR --> NM --> UV --> PR --> BL --> OUT
```

### Network: who talks to whom

```mermaid
flowchart LR
    subgraph WiFi["Same Wi-Fi network"]
        Phone["📱 Phone<br/>https://192.168.x.x:8443"]
        Mac["💻 Mac running serve.py"]
    end
    Laptop["Browser on the Mac<br/>http://localhost:8000"]
    Blender["Blender on the Mac<br/>ws://localhost:8000/ws?client=blender"]

    Phone <-->|HTTPS + SSE| Mac
    Laptop <-->|HTTP + SSE| Mac
    Blender <-->|WebSocket| Mac
    Mac -. "❌ nothing goes to the internet<br/>(except first-time model downloads)" .- Internet((Internet))
```

---

## What you need

| | Requirement | Notes |
| --- | --- | --- |
| 💻 | **Mac with Apple Silicon** (M1, M2, M3, M4 or M5) | Tested on an M5 with 16 GB RAM. More RAM lets you use ULTRA comfortably. |
| 🐍 | **Python 3.12** | `brew install python@3.12`, or [uv](https://docs.astral.sh/uv/). |
| 💾 | **About 25 GB free disk** | Model weights download once. Hunyuan models take about 15 GB; Gemma, Whisper and Kokoro about 5 GB. |
| 📱 | **Any smartphone** (optional) | iPhone (Safari) or Android (Chrome) on the **same Wi-Fi** as the Mac. |
| 🟧 | **Blender 3.0 or newer** (optional) | [blender.org/download](https://www.blender.org/download/) |
| 🤖 | **Ollama** (optional, for talking characters) | [ollama.com](https://ollama.com), then `ollama pull gemma3:4b` |
| 🔧 | **Git** and **OpenSSL** | Both come with macOS / Xcode Command Line Tools (`xcode-select --install`). |

> [!NOTE]
> **Windows and Linux PCs are not supported yet.** The 3D engine uses Apple's GPU (MPS) and the speech-to-text uses Apple MLX. On other systems it would fall back to the CPU and be far too slow. Your phone, however, can be **any** iPhone or Android.

---

## Install on your computer (step by step)

### Step 1: Install the tools

Open **Terminal** (press `⌘ Space`, type *Terminal* and press Enter) and run:

```bash
# Apple's developer tools (git, openssl). Skip if already installed.
xcode-select --install

# Homebrew (skip if you already have it): https://brew.sh
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Python 3.12
brew install python@3.12

# Optional: Ollama for the talking-character AI
brew install ollama
```

### Step 2: Download this project

```bash
cd ~/Downloads
git clone https://github.com/batmandevx/Auto-Blender-Camera-to-blender.git
cd Auto-Blender-Camera-to-blender
```

### Step 3: Create a virtual environment and install packages

```bash
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

This installs PyTorch, the mesh tools, the web server and so on, and takes a few minutes.

### Step 4: Download the Hunyuan3D-2 code

```bash
git clone https://github.com/Tencent-Hunyuan/Hunyuan3D-2 vendor/Hunyuan3D-2
```

### Step 5: Create your settings file

```bash
cp .env.example .env
```

The defaults work for most people. See [Settings](#settings-env) for what you can change.

### Step 6 (optional): Voice and AI chat

```bash
# The AI brain for talking characters
ollama pull gemma3:4b

# The voice: download kokoro-v1.0.onnx and voices-v1.0.bin from
# https://github.com/thewh1teagle/kokoro-onnx/releases
mkdir -p models/kokoro
# then move both files into models/kokoro/
```

Whisper (speech-to-text) downloads itself the first time you talk.

> [!TIP]
> The **3D model weights download automatically** the first time you generate (several GB, safetensors only), so the **first** model takes longer. After that everything works offline.

---

## Start the app

```bash
cd ~/Downloads/Auto-Blender-Camera-to-blender
.venv/bin/python serve.py
```

You'll see something like:

```
  Camera → 3D → Blender
  Laptop:  http://localhost:8000
  Phone:   https://192.168.1.23:8443   (same Wi-Fi; accept the certificate warning once)
  Blender: ws://localhost:8000/ws?client=blender
```

- **On the Mac:** open **http://localhost:8000** in Chrome or Safari.
- **To stop:** press `Ctrl + C` in Terminal.
- `serve.py` also starts Ollama for you if it's installed.

> [!IMPORTANT]
> Keep this Terminal window open while you use the app. Closing it stops the server.

---

## Use it on your smartphone

Your phone uses the Mac as its "brain". The phone only takes photos and shows the result, and all the heavy work happens on the Mac.

```mermaid
flowchart LR
    A["1 · Phone and Mac<br/>on the same Wi-Fi"] --> B["2 · Open the Phone URL<br/>or scan the QR code"]
    B --> C["3 · Accept the certificate<br/>warning (once)"]
    C --> D["4 · Allow camera"]
    D --> E["5 · Add to Home Screen<br/>(optional, works like an app)"]
```

### Step by step

1. **Connect both devices to the same Wi-Fi.** Mobile data, guest networks or VPNs often block this.
2. **Get the address.** It's the `Phone:` line printed by `serve.py`, for example `https://192.168.1.23:8443`.
   - Or on the Mac, open **Gallery → 📱 Open on phone** and **scan the QR code** with your phone camera.
3. **Open it in the phone's browser.** Use **Safari** on iPhone and **Chrome** on Android.
4. **Accept the security warning once.** The app makes its own certificate so your camera can work over Wi-Fi.
   - **iPhone:** tap **Show Details**, then **visit this website**, then **Visit Website**.
   - **Android:** tap **Advanced**, then **Proceed to 192.168.x.x (unsafe)**.
5. **Tap "Tap to start the camera"** and **allow camera access**.
6. *(Optional)* **Add to Home Screen** so it opens full-screen like an app:
   - **iPhone:** Share button, then **Add to Home Screen**.
   - **Android:** ⋮ menu, then **Add to Home screen** / **Install app**.

<p align="center">
  <img src="docs/images/mobile-home.jpg" width="230" alt="Phone camera screen">
  &nbsp;
  <img src="docs/images/mobile-model.jpg" width="230" alt="Phone model screen">
  &nbsp;
  <img src="docs/images/mobile-play.jpg" width="230" alt="Phone play screen">
</p>

> [!WARNING]
> Always use the **`https://`** address on the phone. With `http://` the browser will **not** allow the camera.

---

## Make your first 3D model

### Option A: one photo (fastest)

1. Pick **Photo** at the top.
2. Choose a **quality** (start with **Standard**). Leave **Texture ✓** on.
3. Point the camera at the object and press the **big white shutter button**. You can also tap the 🖼️ button to **upload** a photo, or drag one onto the window on a laptop.
4. **Tap the object** in the photo. A cut-out mask appears.
   - **＋ Keep**: tap more parts to include them.
   - **－ Remove**: tap parts you don't want (a hand, the table).
   - **✨ Auto**: let the app cut it out automatically.
5. Press **Generate**.
6. Watch the progress bar: *Loading model → Sculpting the shape → Cleaning up the mesh → Unwrapping UVs → Painting texture → Done*.
7. An **untextured preview** appears first, then the **final textured model**. Drag it to rotate.

### Option B: 4-VIEW (best quality, real back side)

1. Pick **4-View** at the top.
2. Follow the hint: shoot the **Front**, then **Left**, **Back** and **Right**. Walk around the object, or turn it about 90° between shots.
3. Tap the object in each photo to cut it out.
4. When all four slots are filled, press **Generate 3D**.

```mermaid
flowchart LR
    F["📷 Front"] --> L["📷 Left<br/>(turn 90°)"] --> Bk["📷 Back<br/>(turn 90°)"] --> R["📷 Right<br/>(turn 90°)"] --> G["Generate 3D"]
```

### After the model is ready

| Button | What it does |
| --- | --- |
| **▶ Play · AR · Talk** | Opens the play screen: 3D, hand tracking, AR and chat |
| **Detail…** | Redo cleanup + texture at 20k / 60k / 150k / 250k faces, or untextured, in seconds and without regenerating |
| **🎲 Variation** | A different take from the same photos (new random seed) |
| **✎ Rename** | Give the scan a name |
| **+ New scan** | Back to the camera |
| **Send to Blender** | Push it into Blender right away |

---

## Send the model to Blender

### One-time setup (2 minutes)

```mermaid
flowchart LR
    A["1 · Download add-on<br/>(Blender tab → ⬇ Add-on)"] --> B["2 · Blender: Edit → Preferences<br/>→ Add-ons → Install"]
    B --> C["3 · Enable<br/>'WebSocket Auto Import'"]
    C --> D["4 · 3D view: press N<br/>→ WS Import tab"]
    D --> E["5 · Paste URL → Connect"]
    E --> F["🟢 Connected"]
```

1. **Get the add-on.** In the web app, open a model, go to the **Blender** tab and click **⬇ Add-on**. You can also use [`ws_import_addon.zip`](ws_import_addon.zip) from this repo.
2. In Blender go to **Edit → Preferences → Add-ons → Install…** (in Blender 4.2+: the **⌄** menu at the top right, then **Install from Disk…**) and pick the zip.
3. Tick the checkbox to **enable "WebSocket Auto Import"**.
4. In the 3D viewport, press **`N`** to open the sidebar and click the **WS Import** tab.
   - If it says *Missing websocket-client*, click **Install websocket-client** and wait a moment.
5. The URL should be `ws://localhost:8000/ws?client=blender` (the **copy** button in the Blender tab gives you this). Click **Connect**.
6. The web app's top-right badge turns from 🔴 **Blender: Disconnected** to 🟢 **Connected**.

![Blender tab in the web app](docs/images/desktop-tab-blender.jpg)

### Sending a model

1. Open any model and go to the **Blender** tab.
2. Choose **an animation**, or *Still (no animation)*.
3. Choose **a real-world size**: *Keep size*, 10 cm, 30 cm, 1 m or 1.8 m (human height).
4. Click **Send to Blender**. The model appears in your scene a moment later.

**Import options in the Blender panel:** place at the 3D cursor, sit on the ground, normalize size, decimate, smooth shading, add a shadow-catcher floor. The panel also has **History** and a **Server Gallery** so you can re-import any old scan.

> [!TIP]
> The phone can send to Blender too. Take the photo on the phone, press **Send to Blender**, and the model lands in Blender on your Mac.

---

## Animate it in Blender

### Option 1: use a baked animation (no skills needed)

Pick one of the **18 built-in animations** in the **Blender** tab before sending: *wave, dance, walk, run, hop, backflip, spin, nod, celebrate, sleep, swim, think* and more. The add-on sets the **timeline length** to match, so just press **Space** to play it.

### Option 2: animate it yourself

Every model arrives as a normal, **textured mesh** that's centred and standing on the floor, so all of Blender's tools work on it:

| Goal | How |
| --- | --- |
| **Spin / move / scale** | Select the object, press `I` → *Location/Rotation/Scale* to add a keyframe, go to another frame, change it, then press `I` again. |
| **Turntable render** | Add an **Empty** at the centre, parent a camera to it and key the empty's Z-rotation 0° → 360°. |
| **Make a character walk** | Use **Rigify** (Add → Armature → Human (Meta-Rig)), fit the bones, then **Ctrl P → With Automatic Weights**, or upload the GLB to [Mixamo](https://www.mixamo.com) for auto-rigging and motion. |
| **Physics** | **Physics** tab → Rigid Body to drop it, throw it or knock it over. |
| **Better looks** | Switch to **Cycles**, add an HDRI in *World*, and play with *Roughness/Metallic* in the material. |
| **3D print** | Export as STL from the web app (scaled to 100 mm), or clean it in Blender with the *3D-Print Toolbox* add-on. |

```mermaid
flowchart LR
    M["Model in Blender"] --> K["Keyframes<br/>(I key)"]
    M --> R["Rig<br/>Rigify / Mixamo"]
    M --> P["Physics<br/>Rigid body"]
    M --> S["Shading<br/>Cycles + HDRI"]
    K & R & P & S --> Out["🎬 Render video<br/>or 🎮 export to games"]
```

---

## Play, AR and talking characters

Open **▶ Play · AR · Talk** on any model. The local AI (Gemma 3) looks at your photo and picks a mode automatically, and you can switch it at the top.

| Mode | What you can do |
| --- | --- |
| 🧸 **Character** (dolls, plushies, figures) | Wave, dance, jump, flip, party, nod, no, sleep, spin, follow. **Talk to it** with the 🎤 or by typing. It answers in character and speaks with a natural voice. |
| 🔫 **Gun** (anything gun-like) | Pop-up **targets**, score, muzzle flash, tracers, recoil, shell ejection, gunshot/reload sounds, 12-round magazine. |
| 📦 **Object** | Tumble, bounce, roll, spin, hop, toss, or grab and throw it. |

| View | How it works |
| --- | --- |
| **3D** | Drag to rotate 360° on any axis; buttons for Turntable, Reset and size. **Move** lets you drag and throw it. |
| **✋ Hands** | Uses any camera (laptop, iPhone, Android). **Pinch** to grab, **two-hand pinch** to resize, **poke** with a finger, **wave** to get a wave back. In gun mode, **curl your index finger** to fire and make a **fist** to reload. |
| **Room AR** | Android Chrome with ARCore: place it on your floor, tap to make it walk there, **Follow** to make it walk after you. |
| **iPhone AR** | AR Quick Look with any of the 18 baked animations. |

```mermaid
sequenceDiagram
    actor You
    participant Play as Play screen
    participant W as Whisper (MLX)
    participant G as Gemma 3 (Ollama)
    participant K as Kokoro TTS
    You->>Play: 🎤 "Hi! What's your name?"
    Play->>W: audio
    W-->>Play: text
    Play->>G: text + who the character is
    G-->>Play: reply + action ("wave")
    Play->>K: reply text
    K-->>Play: voice (WAV)
    Play-->>You: Speaks, bounces and waves
```

---

## Edit and export

**Edit tab:** rotate 90° on X/Y/Z, mirror, brightness, contrast, saturation, hue, roughness, metallic and surface smoothing. Every change is saved as a new version, and **Undo** is one click.

**Export tab:**

| Format | Best for |
| --- | --- |
| **GLB** | Blender, web, game engines (Unity, Unreal, Godot) |
| **OBJ** (zip with texture) | Older 3D apps |
| **STL** (100 mm) | 3D printing |
| **PLY** | Point-cloud and mesh tools |
| **USDZ** | iPhone / iPad, Reality Composer |
| **Animated GLB** | The animation you picked in the Blender tab |

![Export tab](docs/images/desktop-tab-export.jpg)

---

## Quality presets and timing

| Preset | Faces | Texture | About how long (M5, 16 GB) | Use it for |
| --- | --- | --- | --- | --- |
| **Draft** | 20k | 1K | about 30 s | Quick tests |
| **Standard** | 60k | 2K | about 45 s | Most things ✅ |
| **High** | 150k | 2K, finer grid | 1–2 min | Detailed objects |
| **Ultra** | 250k | 4K, full-size model | 4–11 min | Hero shots |

> [!NOTE]
> - The **first** generation also downloads the model weights, so it's much slower once.
> - Times grow with **4-VIEW** and with **Ultra**. On Ultra the UV-unwrap step can take several minutes on its own, and the progress bar may sit at **55%** during that time. **It is still working**, so leave it running.

---

## Tips for great scans

- 🔆 **Even, soft light.** Avoid harsh shadows and shiny reflections.
- 🖼️ **Fill the frame** with the object, with a little space around it.
- 🧱 **Plain background** where possible. Tap-to-select handles clutter, but plain is easier.
- 🔄 Use **4-VIEW** for anything that looks different from behind.
- 🎯 Hold the camera **level** with the object's middle.
- ✋ If you're holding the object, tap your hand with **－ Remove**.
- 🔁 Not happy? Try **🎲 Variation**, or **Detail…** to change density without re-generating.

---

## Troubleshooting

| Problem | Fix |
| --- | --- |
| **Phone camera won't open** | Use the **`https://…:8443`** address, not `http://`, and accept the certificate warning. |
| **Phone can't reach the Mac** | Put both on the **same Wi-Fi** and turn off VPN. Allow Python through the macOS firewall (*System Settings → Network → Firewall*). |
| **"Address already in use"** | The server is already running. Open http://localhost:8000, or stop the old one: `lsof -ti tcp:8000 \| xargs kill`. |
| **Stuck at 55% "Cleaning up the mesh…"** | Usually it's the UV unwrap on **High/Ultra**, which can take minutes. Watch the Terminal; when it prints `[Local3D] ✓ …` it's done. Use **Standard** for speed. |
| **Page doesn't load while generating** | The Mac is busy with a heavy step. Wait for it to finish, then refresh. |
| **"Failed to send to Blender"** | Check the add-on panel says **Connected** and the URL is `ws://localhost:8000/ws?client=blender`. |
| ***Missing websocket-client*** in Blender | Click **Install websocket-client** in the WS Import panel, then **Connect**. |
| **AR button does nothing (Android)** | Install **Google Play Services for AR** (ARCore) and use Chrome. |
| **Character doesn't talk** | Install Ollama and run `ollama pull gemma3:4b`. Put the Kokoro files in `models/kokoro/`. |
| **First model is very slow** | That's the one-time model download (several GB). Later runs are fast. |

---

## Settings (`.env`)

| Variable | Default | Description |
| --- | --- | --- |
| `PORT` | `8000` | HTTP port (laptop browser + Blender) |
| `HTTPS_PORT` | `8443` | HTTPS port (phones) |
| `BG_REMOVAL` | `rembg` | `rembg` or `none` |
| `REMBG_MODEL` | `isnet-general-use` | `birefnet-general` (cleaner edges, slower), `u2net` |
| `SAM_MODEL` | `sam` | Tap-to-select model |
| `HY3DGEN_MODELS` | `~/.cache/hy3dgen` | Where the Hunyuan weights are stored |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Local Ollama server |
| `AI_MODEL` | `gemma3:4b` | Chat / vision model |
| `WHISPER_MODEL` | `mlx-community/whisper-large-v3-turbo` | Speech-to-text model |

Run the server without HTTPS (laptop only): `.venv/bin/python serve.py --no-https`

---

## API reference

All endpoints are served by [`relay_server.py`](relay_server.py).

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/isolate` | Automatic cut-out (rembg) |
| `POST` | `/segment` | Tap-to-select cut-out (SAM) |
| `POST` | `/generate3d` | Photo(s) → 3D job (`image`, or `front` + `left`/`back`/`right`) |
| `GET` | `/jobs/{id}/stream` | Live progress (Server-Sent Events: `progress`, `preview`, `complete`, `error`) |
| `GET` | `/scans` · `/scans/{id}` | Gallery / one scan |
| `PATCH` · `DELETE` | `/scans/{id}` | Rename / delete |
| `POST` | `/scans/{id}/refine` | Redo cleanup + texture at a new detail level |
| `POST` | `/scans/{id}/variation` | Regenerate with a new seed |
| `POST` | `/scans/{id}/edit` · `/undo` | Edit (rotate, colour, material) / undo |
| `GET` | `/scans/{id}/export/{fmt}` | Download `glb` / `obj` / `stl` / `ply` |
| `POST` | `/scans/{id}/ar` | Animated GLB + USDZ at real-world size |
| `POST` | `/scans/{id}/identify` · `/role` · `/chat` | Local AI: identify, set role, chat |
| `POST` | `/ai/stt` · `/ai/tts` | Speech-to-text / text-to-speech |
| `POST` | `/relay` | Push a model to connected Blender clients |
| `WS` | `/ws?client=blender` | WebSocket for Blender |
| `GET` | `/engines` · `/health` · `/connection-info` · `/addon.zip` | Info, health, phone URL/QR, add-on download |

---

## Project structure

```
.
├── serve.py              Starts HTTP :8000 + HTTPS :8443 in one process (makes the cert, starts Ollama)
├── relay_server.py       FastAPI app: cut-outs, jobs, gallery, exports, AR, AI, WebSocket relay to Blender
├── local3d.py            Hunyuan3D-2 engine, job queue, quality presets, mesh cleanup
├── texture_project.py    UV unwrap (xatlas) + multi-view photo projection → texture
├── ar_export.py          18 baked animations → animated GLB + USDZ at real-world size
├── ai_local.py           Gemma 3 (identify/chat), Whisper (STT), Kokoro (TTS), all local
├── webapp/               Capture app (index.html, app.js, app.css), Play (play.html, play.js), theme.css
│   └── vendor/mediapipe/ Hand tracking (served locally, works offline)
├── blender_addon/        Blender add-on source ("WebSocket Auto Import")
├── ws_import_addon.zip   Installable copy of the add-on
├── docs/images/          README screenshots
├── requirements.txt      Python dependencies
└── .env.example          Settings template

Created at runtime (git-ignored):
├── vendor/Hunyuan3D-2/   Hunyuan3D-2 checkout
├── models/kokoro/        Voice model files
├── scans/                Your saved scans (photos, GLBs, metadata)
└── certs/                Self-signed HTTPS certificate
```

---

## License

**MIT**. See [LICENSE](LICENSE).

The bundled and downloaded models have their own licenses:

- **Hunyuan3D-2**: Tencent Hunyuan Community License. It does **not** cover the EU, UK or South Korea.
- **Gemma 3**: Gemma Terms of Use.
- **Whisper**: MIT · **Kokoro**: Apache 2.0 · **MediaPipe**: Apache 2.0 · **SAM**: Apache 2.0.

<div align="center">

Made for people who want to turn real things into 3D, without the cloud. ⭐ Star the repo if it helped you!

</div>
