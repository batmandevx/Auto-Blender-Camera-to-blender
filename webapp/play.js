// Play: bring a scan to life.
//   3D     free 360° rotation, drag/throw, all animations
//   Hands  camera + finger tracking (MediaPipe): pinch to grab, two hands to resize/turn,
//          poke, wave, hold a gun and pull the trigger with your index finger
//   Room   WebXR on Android: place on the floor, walk, follow, shoot targets in your room
//   iPhone AR Quick Look with a baked animation
// Characters talk with local AI (Gemma 3 via Ollama, Whisper STT, Kokoro TTS).
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const API = location.origin;
const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const scanId = params.get('scan');

// ── State ────────────────────────────────────────────────────────────────
let scan = null;
let role = 'object';          // character | gun | object
let mode = '3d';              // 3d | hand | room | quicklook
let tool = 'rotate';          // rotate | move  (3D mode drag behaviour)
let placed = true;
let turntable = false;
let userScale = 1;
let baseScale = 1;
let modelHeight = 1;
let modelSize = new THREE.Vector3(1, 1, 1);
let action = null;            // { type, t0, dur }
let walkTarget = null;
let following = false;
let talking = null;           // { an, data } while the character speaks
let chatHistory = [];
let busyChat = false;
let gun = null;               // { muzzle, dir, grip } in model-local space
let ammo = 12;
let hits = 0;
let lastShot = 0;
const AMMO_MAX = 12;

// Physics for drag / throw / drops
const phys = { vel: new THREE.Vector3(), spin: 0, airborne: false, grabbed: false };

// ── Renderer / scene ─────────────────────────────────────────────────────
const canvas = $('stage');
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.toneMapping = THREE.NeutralToneMapping;
renderer.toneMappingExposure = 1.15;

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(55, window.innerWidth / window.innerHeight, 0.01, 60);
camera.position.set(0.55, 0.42, 0.85);

scene.add(new THREE.HemisphereLight(0xffffff, 0x4a4a55, 2.6));
const sun = new THREE.DirectionalLight(0xffffff, 1.8);
sun.position.set(1, 3, 2);
scene.add(sun);

// root: where it stands + which way it faces + size
// turn: user's free 360° rotation (quaternion)
// anim: procedural animation offsets
const root = new THREE.Group();
const turn = new THREE.Group();
const anim = new THREE.Group();
root.add(turn);
turn.add(anim);
scene.add(root);

function makeShadow() {
  const c = document.createElement('canvas');
  c.width = c.height = 128;
  const g = c.getContext('2d');
  const grad = g.createRadialGradient(64, 64, 4, 64, 64, 64);
  grad.addColorStop(0, 'rgba(0,0,0,0.55)');
  grad.addColorStop(1, 'rgba(0,0,0,0)');
  g.fillStyle = grad;
  g.fillRect(0, 0, 128, 128);
  const m = new THREE.Mesh(new THREE.PlaneGeometry(1, 1),
    new THREE.MeshBasicMaterial({ map: new THREE.CanvasTexture(c), transparent: true, depthWrite: false }));
  m.rotation.x = -Math.PI / 2;
  m.renderOrder = -1;
  return m;
}
const shadow = makeShadow();
scene.add(shadow);

const floor = new THREE.Group();
const floorDisc = new THREE.Mesh(new THREE.CircleGeometry(3, 64), new THREE.MeshStandardMaterial({ color: 0x121217, roughness: 1 }));
floorDisc.rotation.x = -Math.PI / 2;
floor.add(floorDisc);
const grid = new THREE.GridHelper(6, 30, 0x2a2a36, 0x1d1d26);
grid.position.y = 0.001;
floor.add(grid);
scene.add(floor);

const reticle = new THREE.Mesh(new THREE.RingGeometry(0.06, 0.075, 40).rotateX(-Math.PI / 2),
  new THREE.MeshBasicMaterial({ color: 0x7c5cff, transparent: true, opacity: 0.9 }));
reticle.matrixAutoUpdate = false;
reticle.visible = false;
scene.add(reticle);

const controls = new OrbitControls(camera, canvas);
controls.target.set(0, 0.14, 0);
controls.enableDamping = true;
controls.minDistance = 0.2;
controls.maxDistance = 5;
controls.minPolarAngle = 0;
controls.maxPolarAngle = Math.PI;          // full 360° orbit, including from below

// Floor height (3D/room = 0; hand mode uses a virtual floor below the view centre)
let floorY = 0;

// ── UI helpers ───────────────────────────────────────────────────────────
function hint(text, ms = 0) {
  $('hint').textContent = text || '';
  if (ms) setTimeout(() => { if ($('hint').textContent === text) $('hint').textContent = ''; }, ms);
}
let toastTimer;
function toast(text, error = false) {
  let el = document.querySelector('.toast');
  if (!el) { el = document.createElement('div'); el.className = 'toast'; document.body.appendChild(el); }
  el.textContent = text;
  el.classList.toggle('error', error);
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), error ? 5000 : 2400);
}
function show(id, on = true) { $(id).classList.toggle('hidden', !on); }
function heightMetres() { return modelHeight * baseScale * userScale; }
function applyScale() {
  root.scale.setScalar(baseScale * userScale);
  $('sizeVal').textContent = `${Math.round(heightMetres() * 100)} cm`;
}
async function postJSON(path, body) {
  const res = await fetch(`${API}${path}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res;
}

// ── Sound effects (synthesised, no files) ────────────────────────────────
let actx = null;
function ac() {
  if (!actx) actx = new (window.AudioContext || window.webkitAudioContext)();
  if (actx.state === 'suspended') actx.resume();
  return actx;
}
function noise(a, dur) {
  const b = a.createBuffer(1, Math.floor(a.sampleRate * dur), a.sampleRate);
  const d = b.getChannelData(0);
  for (let i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1;
  return b;
}
function env(a, node, t, peak, decay) {
  const g = a.createGain();
  g.gain.setValueAtTime(peak, t);
  g.gain.exponentialRampToValueAtTime(0.0008, t + decay);
  node.connect(g).connect(a.destination);
  return g;
}
const sfx = {
  shot() {
    const a = ac(), t = a.currentTime;
    const n = a.createBufferSource(); n.buffer = noise(a, 0.5);
    const f = a.createBiquadFilter(); f.type = 'lowpass';
    f.frequency.setValueAtTime(7000, t); f.frequency.exponentialRampToValueAtTime(300, t + 0.3);
    n.connect(f); env(a, f, t, 1.4, 0.42); n.start(t);
    const o = a.createOscillator(); o.type = 'sine';
    o.frequency.setValueAtTime(150, t); o.frequency.exponentialRampToValueAtTime(38, t + 0.18);
    env(a, o, t, 1.2, 0.22); o.start(t); o.stop(t + 0.25);
    setTimeout(() => sfx.shell(), 260);
  },
  shell() {
    const a = ac(), t = a.currentTime;
    [0, 0.09, 0.16].forEach((dt, i) => {
      const o = a.createOscillator(); o.type = 'triangle'; o.frequency.setValueAtTime(3200 - i * 400, t + dt);
      env(a, o, t + dt, 0.12 / (i + 1), 0.06); o.start(t + dt); o.stop(t + dt + 0.08);
    });
  },
  click() {
    const a = ac(), t = a.currentTime;
    const n = a.createBufferSource(); n.buffer = noise(a, 0.05);
    const f = a.createBiquadFilter(); f.type = 'highpass'; f.frequency.value = 2500;
    n.connect(f); env(a, f, t, 0.5, 0.04); n.start(t);
  },
  reload() { sfx.click(); setTimeout(sfx.click, 180); setTimeout(() => { sfx.click(); sfx.shell(); }, 520); },
  pop() {
    const a = ac(), t = a.currentTime;
    const o = a.createOscillator(); o.type = 'square';
    o.frequency.setValueAtTime(900, t); o.frequency.exponentialRampToValueAtTime(180, t + 0.15);
    env(a, o, t, 0.25, 0.18); o.start(t); o.stop(t + 0.2);
  },
  boing() {
    const a = ac(), t = a.currentTime;
    const o = a.createOscillator(); o.type = 'sine';
    o.frequency.setValueAtTime(220, t); o.frequency.exponentialRampToValueAtTime(660, t + 0.12); o.frequency.exponentialRampToValueAtTime(330, t + 0.35);
    env(a, o, t, 0.35, 0.4); o.start(t); o.stop(t + 0.42);
  },
  whoosh() {
    const a = ac(), t = a.currentTime;
    const n = a.createBufferSource(); n.buffer = noise(a, 0.4);
    const f = a.createBiquadFilter(); f.type = 'bandpass'; f.Q.value = 1.2;
    f.frequency.setValueAtTime(400, t); f.frequency.exponentialRampToValueAtTime(2400, t + 0.3);
    n.connect(f); env(a, f, t, 0.35, 0.35); n.start(t);
  },
  thud() {
    const a = ac(), t = a.currentTime;
    const o = a.createOscillator(); o.type = 'sine';
    o.frequency.setValueAtTime(110, t); o.frequency.exponentialRampToValueAtTime(45, t + 0.12);
    env(a, o, t, 0.5, 0.16); o.start(t); o.stop(t + 0.18);
  },
};

// ── Load scan ────────────────────────────────────────────────────────────
async function loadScan() {
  if (!scanId) throw new Error('No scan selected. Open Play from a model in the app.');
  const res = await fetch(`${API}/scans/${encodeURIComponent(scanId)}`);
  if (!res.ok) throw new Error('Scan not found');
  scan = await res.json();
  if (!scan.model_url) throw new Error('This scan has no finished model yet');
  $('title').textContent = (scan.ai && scan.ai.name) || scan.name || 'Scan';
  $('subtitle').textContent = (scan.ai && scan.ai.what) || 'Hunyuan3D';
  document.title = `Play · ${$('title').textContent}`;
  role = (scan.ai && scan.ai.role) || params.get('role') || 'object';

  const gltf = await new GLTFLoader().loadAsync(`${API}${scan.model_url}?v=${scan.updated || 0}`);
  const model = gltf.scene;
  model.traverse(o => { if (o.isMesh) o.frustumCulled = false; });
  const box = new THREE.Box3().setFromObject(model);
  modelSize = box.getSize(new THREE.Vector3());
  const c = box.getCenter(new THREE.Vector3());
  model.position.set(-c.x, -box.min.y, -c.z);
  anim.add(model);
  modelHeight = Math.max(modelSize.y, 1e-3);
  analyzeGun(model);
  setBaseScale();
}

function setBaseScale() {
  // Guns are sized by length (~25 cm), everything else by height (~30 cm).
  if (role === 'gun' && gun) baseScale = 0.25 / gun.length;
  else baseScale = 0.3 / modelHeight;
  applyScale();
}

// Find barrel, muzzle and grip from the shape: the barrel end is the thinner end of the long axis.
function analyzeGun(model) {
  const pts = [];
  model.updateMatrixWorld(true);
  model.traverse(o => {
    if (!o.isMesh) return;
    const pos = o.geometry.attributes.position;
    const step = Math.max(1, Math.floor(pos.count / 8000));
    const v = new THREE.Vector3();
    for (let i = 0; i < pos.count; i += step) pts.push(v.fromBufferAttribute(pos, i).applyMatrix4(o.matrixWorld).clone());
  });
  if (!pts.length) return;
  const box = new THREE.Box3().setFromPoints(pts);
  const size = box.getSize(new THREE.Vector3());
  const axis = size.x >= size.z ? 'x' : 'z';
  const lo = box.min[axis], hi = box.max[axis], L = hi - lo;
  const endStats = (near) => {
    const sel = pts.filter(p => near ? p[axis] < lo + 0.15 * L : p[axis] > hi - 0.15 * L);
    const ys = sel.map(p => p.y);
    const c = sel.reduce((a, p) => a.add(p), new THREE.Vector3()).multiplyScalar(1 / Math.max(sel.length, 1));
    return { h: Math.max(...ys) - Math.min(...ys), c };
  };
  const a = endStats(true), b = endStats(false);
  const muzzleAtHigh = b.h < a.h;
  const dir = new THREE.Vector3(); dir[axis] = muzzleAtHigh ? 1 : -1;
  const hiEnd = b.c.clone(); hiEnd[axis] = hi;
  const loEnd = a.c.clone(); loEnd[axis] = lo;
  const lowPts = pts.filter(p => p.y < box.min.y + 0.35 * size.y);
  const grip = lowPts.reduce((s, p) => s.add(p), new THREE.Vector3()).multiplyScalar(1 / Math.max(lowPts.length, 1));
  gun = { muzzle: muzzleAtHigh ? hiEnd : loEnd, other: muzzleAtHigh ? loEnd : hiEnd, dir, grip, length: L };
}
function flipBarrel() {
  if (!gun) return;
  gun.dir.negate();
  [gun.muzzle, gun.other] = [gun.other, gun.muzzle];
  toast('Barrel flipped');
}

// ── Animations ───────────────────────────────────────────────────────────
const ACTIONS = {
  jump: 0.9, spin: 1.1, spin360: 1.4, dance: 3.2, wobble: 1.4, happy: 1.0, wave: 1.6, nod: 1.2, shake: 1.2,
  backflip: 1.4, celebrate: 1.8, sad: 2.0, think: 2.4, sleep: 4.0, bounce: 1.8, roll: 1.6, recoil: 0.32, tumble: 1.2,
};
function play(type) {
  if (!ACTIONS[type]) return;
  action = { type, t0: performance.now() / 1000, dur: ACTIONS[type] };
  if (['jump', 'backflip', 'bounce', 'celebrate'].includes(type)) sfx.whoosh();
  if (type === 'happy') sfx.boing();
}
const ease = (p) => (p < 0.5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2);

function applyAnimation(t, walking) {
  const H = modelHeight;
  anim.position.set(0, 0, 0);
  anim.rotation.set(0, 0, 0);
  anim.scale.set(1, 1, 1);
  if (action) {
    const p = (t - action.t0) / action.dur;
    if (p >= 1) action = null;
    else {
      const hop = Math.sin(Math.PI * p);
      switch (action.type) {
        case 'jump': {
          anim.position.y = 0.55 * H * hop;
          const sq = p < 0.12 ? (0.12 - p) / 0.12 : p > 0.9 ? (p - 0.9) / 0.1 : 0;
          anim.scale.set(1 + 0.1 * sq, 1 - 0.15 * sq + 0.05 * hop, 1 + 0.1 * sq);
          break;
        }
        case 'spin': anim.rotation.y = 2 * Math.PI * ease(p); anim.position.y = 0.12 * H * hop; break;
        case 'spin360': anim.rotation.y = 2 * Math.PI * ease(p); break;
        case 'dance':
          anim.rotation.y = 0.5 * Math.sin(2 * Math.PI * 2 * p);
          anim.rotation.z = 0.18 * Math.sin(2 * Math.PI * 4 * p);
          anim.position.y = 0.06 * H * Math.abs(Math.sin(2 * Math.PI * 4 * p));
          break;
        case 'wobble':
          anim.rotation.z = 0.28 * Math.sin(2 * Math.PI * 3 * p) * (1 - p);
          anim.rotation.x = 0.08 * Math.sin(2 * Math.PI * 3 * p + 1) * (1 - p);
          break;
        case 'happy': anim.position.y = 0.35 * H * hop; anim.rotation.y = 2 * Math.PI * p; break;
        case 'wave': anim.rotation.z = 0.24 * Math.sin(2 * Math.PI * 3 * p); anim.position.y = 0.03 * H * Math.abs(Math.sin(2 * Math.PI * 3 * p)); break;
        case 'nod': anim.rotation.x = 0.22 * Math.sin(2 * Math.PI * 2 * p); break;
        case 'shake': anim.rotation.y = 0.38 * Math.sin(2 * Math.PI * 3 * p) * (1 - p * 0.5); break;
        case 'backflip': anim.position.y = 0.7 * H * hop; anim.rotation.x = -2 * Math.PI * ease(Math.min(Math.max((p - 0.08) / 0.8, 0), 1)); break;
        case 'celebrate':
          anim.position.y = 0.3 * H * Math.abs(Math.sin(3 * Math.PI * p));
          anim.rotation.y = 2 * Math.PI * p;
          anim.scale.setScalar(1 + 0.06 * Math.abs(Math.sin(3 * Math.PI * p)));
          break;
        case 'sad': anim.rotation.x = 0.25 * Math.min(p * 4, 1) * (1 - Math.max(p - 0.8, 0) * 5); anim.scale.y = 1 - 0.06 * hop; break;
        case 'think': anim.rotation.z = 0.1 * Math.min(p * 4, 1); anim.rotation.y = 0.25 * Math.sin(2 * Math.PI * p); break;
        case 'sleep': anim.rotation.z = -0.25 * Math.min(p * 3, 1) * (1 - Math.max(p - 0.85, 0) * 6.6); anim.scale.y = 1 - 0.03 * Math.sin(2 * Math.PI * 2 * p); break;
        case 'bounce': anim.position.y = 0.3 * H * Math.abs(Math.sin(3 * Math.PI * p)) * (1 - p); break;
        case 'roll': anim.rotation.z = -2 * Math.PI * ease(p); anim.position.y = 0.4 * H * hop; break;
        case 'tumble': anim.rotation.x = 2 * Math.PI * p; break;
        case 'recoil': {
          const k = Math.exp(-10 * p) * Math.sin(Math.PI * Math.min(p * 3, 1));
          const back = gun ? gun.dir.clone().multiplyScalar(-0.12 * gun.length * k) : new THREE.Vector3();
          anim.position.add(back);
          anim.rotation.z += (gun && gun.dir.x !== 0 ? Math.sign(gun.dir.x) : 1) * 0.35 * k;
          anim.rotation.x += (gun && gun.dir.z !== 0 ? -Math.sign(gun.dir.z) : 0) * 0.35 * k;
          break;
        }
      }
    }
  } else if (walking) {
    const w = t * 11;
    anim.rotation.z = 0.12 * Math.sin(w);
    anim.position.y = 0.045 * H * Math.abs(Math.sin(w));
  } else if (role === 'gun') {
    if (mode === '3d') anim.position.y = 0.01 * modelHeight * Math.sin(t * 1.4);   // hovering on its stand
  } else if (!phys.grabbed) {
    const b = Math.sin(t * 2.2);
    anim.scale.set(1 - 0.01 * b, 1 + 0.025 * b, 1 - 0.01 * b);   // breathing
  }
  // Talking: bounce with the voice
  if (talking) {
    talking.an.getByteTimeDomainData(talking.data);
    let sum = 0;
    for (let i = 0; i < talking.data.length; i++) { const v = (talking.data[i] - 128) / 128; sum += v * v; }
    const level = Math.min(Math.sqrt(sum / talking.data.length) * 5, 1);
    talking.level = (talking.level || 0) * 0.6 + level * 0.4;
    anim.scale.y *= 1 + 0.08 * talking.level;
    anim.scale.x *= 1 - 0.03 * talking.level;
    anim.rotation.z += 0.05 * Math.sin(t * 9) * talking.level;
  }
}

// ── Behaviour loop ───────────────────────────────────────────────────────
function lerpAngle(a, b, t) {
  let d = ((b - a + Math.PI) % (2 * Math.PI)) - Math.PI;
  if (d < -Math.PI) d += 2 * Math.PI;
  return a + d * t;
}
function viewerPosition() {
  return mode === 'room' ? renderer.xr.getCamera().getWorldPosition(new THREE.Vector3()) : camera.position.clone();
}
function faceViewer() {
  const v = viewerPosition();
  root.rotation.y = Math.atan2(v.x - root.position.x, v.z - root.position.z);
}

function update(dt, t) {
  if (!placed) { shadow.visible = false; return; }
  const hM = heightMetres();

  if (turntable && !phys.grabbed) turn.rotateOnWorldAxis(new THREE.Vector3(0, 1, 0), dt * 0.9);

  if (following && mode !== 'hand') {
    const v = viewerPosition();
    const tgt = new THREE.Vector3(v.x, root.position.y, v.z);
    const keep = 0.45 + hM;
    const d = tgt.distanceTo(root.position);
    walkTarget = d > keep + 0.05 ? root.position.clone().add(tgt.sub(root.position).setLength(d - keep)) : null;
  }

  let walking = false;
  if (walkTarget && !phys.grabbed && !phys.airborne) {
    const dir = new THREE.Vector3(walkTarget.x - root.position.x, 0, walkTarget.z - root.position.z);
    const dist = dir.length();
    if (dist < 0.01) walkTarget = null;
    else {
      const desired = Math.atan2(dir.x, dir.z);
      root.rotation.y = lerpAngle(root.rotation.y, desired, 1 - Math.exp(-8 * dt));
      if (Math.abs(lerpAngle(root.rotation.y, desired, 1) - root.rotation.y) < 0.9) {
        root.position.addScaledVector(dir.normalize(), Math.min(dist, Math.max(0.2, 1.3 * hM) * dt));
      }
      walking = true;
    }
  }

  // Throw / drop physics
  if (!phys.grabbed && (phys.airborne || root.position.y > floorY + 1e-4)) {
    phys.airborne = true;
    phys.vel.y -= 9.8 * dt * Math.max(hM / 0.3, 0.35);
    root.position.addScaledVector(phys.vel, dt);
    turn.rotateOnWorldAxis(new THREE.Vector3(0, 1, 0), phys.spin * dt);
    if (root.position.y <= floorY) {
      root.position.y = floorY;
      if (Math.abs(phys.vel.y) > 0.35) {
        phys.vel.y *= -0.42; phys.vel.x *= 0.6; phys.vel.z *= 0.6; phys.spin *= 0.6;
        sfx.thud();
        if (role === 'character') play('wobble');
      } else {
        phys.vel.set(0, 0, 0); phys.spin = 0; phys.airborne = false;
      }
    }
  }

  applyAnimation(t, walking);
  if (mode === 'hand' && handHold.active && role === 'gun') positionGunInHand();

  // Shadow on the floor under the model
  shadow.visible = mode !== 'room' || placed;
  const w = Math.max(modelSize.x, modelSize.z) * 1.5 * baseScale * userScale;
  const lift = Math.min(Math.max((root.position.y - floorY) + anim.position.y * baseScale * userScale, 0) / Math.max(hM, 0.05), 1);
  shadow.position.set(root.position.x, floorY + 0.002, root.position.z);
  shadow.scale.set(w * (1 - 0.4 * lift), w * (1 - 0.4 * lift), 1);
  shadow.material.opacity = 1 - 0.75 * lift;

  updateEffects(dt);
  updateSpeechBubble();
}

// ── 3D mode: drag to rotate 360° or move/throw ───────────────────────────
const raycaster = new THREE.Raycaster();
function ndcFrom(x, y) { return new THREE.Vector2((x / window.innerWidth) * 2 - 1, -(y / window.innerHeight) * 2 + 1); }
function activeCamera() {
  if (mode === 'room') { const c = renderer.xr.getCamera(); return (c.cameras && c.cameras[0]) || c; }
  return camera;
}
function hitsModel(x, y) {
  raycaster.setFromCamera(ndcFrom(x, y), activeCamera());
  return raycaster.intersectObject(anim, true).length > 0;
}

let drag = null;
canvas.addEventListener('pointerdown', (e) => {
  if (mode !== '3d') return;
  ac();
  const onModel = hitsModel(e.clientX, e.clientY);
  drag = { x: e.clientX, y: e.clientY, sx: e.clientX, sy: e.clientY, onModel, moved: false, hist: [] };
  if (onModel) {
    controls.enabled = false;
    canvas.setPointerCapture(e.pointerId);
    if (tool === 'move') {
      phys.grabbed = true; phys.airborne = false; phys.vel.set(0, 0, 0);
      const plane = new THREE.Plane().setFromNormalAndCoplanarPoint(camera.getWorldDirection(new THREE.Vector3()).negate(), root.position);
      drag.plane = plane;
      raycaster.setFromCamera(ndcFrom(e.clientX, e.clientY), camera);
      const p = new THREE.Vector3(); raycaster.ray.intersectPlane(plane, p);
      drag.offset = root.position.clone().sub(p);
    }
  }
});
canvas.addEventListener('pointermove', (e) => {
  if (!drag || mode !== '3d') return;
  const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
  drag.x = e.clientX; drag.y = e.clientY;
  if (Math.hypot(e.clientX - drag.sx, e.clientY - drag.sy) > 4) drag.moved = true;
  if (!drag.onModel) return;
  if (tool === 'rotate') {
    // Trackball: horizontal drag spins around the screen's up axis, vertical tilts around its right axis.
    const up = new THREE.Vector3(0, 1, 0).applyQuaternion(camera.quaternion);
    const right = new THREE.Vector3(1, 0, 0).applyQuaternion(camera.quaternion);
    turn.parent.updateMatrixWorld();
    const inv = turn.parent.getWorldQuaternion(new THREE.Quaternion()).invert();
    turn.quaternion.premultiply(new THREE.Quaternion().setFromAxisAngle(up.applyQuaternion(inv), dx * 0.01));
    turn.quaternion.premultiply(new THREE.Quaternion().setFromAxisAngle(right.applyQuaternion(inv), dy * 0.01));
  } else if (drag.plane) {
    raycaster.setFromCamera(ndcFrom(e.clientX, e.clientY), camera);
    const p = new THREE.Vector3();
    if (raycaster.ray.intersectPlane(drag.plane, p)) {
      p.add(drag.offset);
      p.y = Math.max(p.y, floorY);
      drag.hist.push({ p: p.clone(), t: performance.now() });
      if (drag.hist.length > 6) drag.hist.shift();
      root.position.copy(p);
    }
  }
});
canvas.addEventListener('pointerup', (e) => {
  if (!drag || mode !== '3d') return;
  const d = drag; drag = null;
  controls.enabled = true;
  if (phys.grabbed) {
    phys.grabbed = false;
    if (d.hist.length >= 2) {
      const a = d.hist[0], b = d.hist[d.hist.length - 1];
      const dtv = Math.max((b.t - a.t) / 1000, 0.016);
      phys.vel.copy(b.p).sub(a.p).multiplyScalar(1 / dtv).clampLength(0, 4);
      phys.spin = (Math.random() - 0.5) * 6 * Math.min(phys.vel.length(), 1);
      if (phys.vel.length() > 0.6) sfx.whoosh();
    }
    phys.airborne = root.position.y > floorY + 1e-3 || phys.vel.y > 0;
  }
  if (!d.moved) onTap(e.clientX, e.clientY, d.onModel);
});
// Gun in 3D: it aims wherever your mouse points (at a target if you're over one).
let aimDir = null;
function aimPointFor(x, y) {
  raycaster.setFromCamera(ndcFrom(x, y), camera);
  const hit = raycaster.intersectObjects(targets.map(t => t.mesh), true)[0];
  if (hit) return hit.point;
  return raycaster.ray.at(camera.position.distanceTo(root.position) * 2.5, new THREE.Vector3());
}
function setAim(x, y) {
  if (!gun) return;
  const { p } = muzzleWorld();
  aimDir = aimPointFor(x, y).sub(p).normalize();
}
canvas.addEventListener('pointermove', (e) => {
  if (mode !== '3d' || role !== 'gun' || drag) return;
  setAim(e.clientX, e.clientY);
});
function aimGun(k = 0.25) {
  if (!aimDir || !gun) return;
  const inv = root.getWorldQuaternion(new THREE.Quaternion()).invert();
  const local = aimDir.clone().applyQuaternion(inv);
  turn.quaternion.slerp(new THREE.Quaternion().setFromUnitVectors(gun.dir.clone().normalize(), local), k);
}

function onTap(x, y, onModel) {
  ac();
  if (role === 'gun') {
    if (mode === '3d') { for (let i = 0; i < 4; i++) { setAim(x, y); aimGun(1); } }
    fire();
    return;
  }
  if (onModel) { play(role === 'character' ? 'happy' : 'tumble'); if (role === 'character') sfx.boing(); return; }
  if (mode === '3d') {
    raycaster.setFromCamera(ndcFrom(x, y), camera);
    const p = new THREE.Vector3();
    if (raycaster.ray.intersectPlane(new THREE.Plane(new THREE.Vector3(0, 1, 0), -floorY), p) && p.length() < 3) {
      following = false; $('btnFollow') && $('btnFollow').classList.remove('is-on');
      walkTarget = p;
    }
  }
}

// ── Gun: shoot, effects, targets ─────────────────────────────────────────
const effects = [];
const targets = [];
let targetsOn = false;

function muzzleWorld() {
  root.updateMatrixWorld(true);
  const p = gun.muzzle.clone().applyMatrix4(anim.matrixWorld);
  const dir = gun.dir.clone().transformDirection(anim.matrixWorld).normalize();
  return { p, dir };
}

function fire() {
  if (role !== 'gun' || !gun) return;
  const now = performance.now();
  if (now - lastShot < 140) return;
  lastShot = now;
  if (ammo <= 0) { sfx.click(); hint('Out of ammo. Tap RELOAD (or make a fist)', 1600); return; }
  ammo--; $('ammo').textContent = ammo;
  sfx.shot();
  play('recoil');
  if (navigator.vibrate) navigator.vibrate(30);

  let origin, dir;
  if (mode === 'room') {
    const c = activeCamera();
    origin = c.getWorldPosition(new THREE.Vector3());
    dir = new THREE.Vector3(0, 0, -1).applyQuaternion(c.getWorldQuaternion(new THREE.Quaternion()));
  } else {
    ({ p: origin, dir } = muzzleWorld());
  }
  const { p: muzzle } = muzzleWorld();
  const s = Math.max(heightMetres(), 0.08);

  // Muzzle flash
  const flash = new THREE.Mesh(new THREE.SphereGeometry(s * 0.18, 12, 12),
    new THREE.MeshBasicMaterial({ color: 0xffc35a, transparent: true, opacity: 1 }));
  flash.position.copy(muzzle).addScaledVector(dir, s * 0.06);
  scene.add(flash);
  const light = new THREE.PointLight(0xffa040, 6, s * 6);
  light.position.copy(flash.position);
  scene.add(light);
  effects.push({ obj: flash, life: 0.07, t: 0, kind: 'flash' }, { obj: light, life: 0.07, t: 0, kind: 'light' });

  // Hit test against targets
  targets.forEach(tg => tg.mesh.updateMatrixWorld(true));
  raycaster.set(origin, dir);
  raycaster.far = 20;
  const hit = raycaster.intersectObjects(targets.map(tg => tg.mesh), true)[0];
  raycaster.far = Infinity;
  const end = hit ? hit.point : origin.clone().addScaledVector(dir, 8);

  // Tracer
  const geom = new THREE.BufferGeometry().setFromPoints([muzzle, end]);
  const tracer = new THREE.Line(geom, new THREE.LineBasicMaterial({ color: 0xfff1b0, transparent: true, opacity: 0.9 }));
  scene.add(tracer);
  effects.push({ obj: tracer, life: 0.09, t: 0, kind: 'fade' });

  // Shell casing
  const shell = new THREE.Mesh(new THREE.CylinderGeometry(s * 0.02, s * 0.02, s * 0.07, 8),
    new THREE.MeshStandardMaterial({ color: 0xd4a73c, metalness: 0.9, roughness: 0.3 }));
  shell.position.copy(muzzle).addScaledVector(dir, -s * 0.6);
  const side = new THREE.Vector3(0, 1, 0).cross(dir).normalize();
  effects.push({ obj: shell, life: 1.2, t: 0, kind: 'shell',
    vel: side.multiplyScalar(0.9 * s / 0.25).add(new THREE.Vector3(0, 1.4 * s / 0.25, 0)), spin: 14 });
  scene.add(shell);

  if (hit) {
    const tg = targets.find(x => hit.object === x.mesh || x.mesh.children.includes(hit.object));
    sparks(hit.point, 0xff5a3c);
    sfx.pop();
    if (tg) { scene.remove(tg.mesh); targets.splice(targets.indexOf(tg), 1); hits++; $('score').textContent = `Targets hit: ${hits}`; setTimeout(spawnTarget, 700); }
  } else if (mode !== 'room') {
    sparks(end, 0xffffff, 0.4);
  }
  if (ammo === 0) setTimeout(() => hint('Empty! Reload', 1400), 200);
}

function reload() {
  if (ammo === AMMO_MAX) return;
  sfx.reload();
  hint('Reloading…', 800);
  setTimeout(() => { ammo = AMMO_MAX; $('ammo').textContent = ammo; }, 600);
}

function sparks(at, color, amount = 1) {
  const n = Math.round(14 * amount);
  const s = Math.max(heightMetres(), 0.08);
  for (let i = 0; i < n; i++) {
    const m = new THREE.Mesh(new THREE.SphereGeometry(s * 0.02, 6, 6), new THREE.MeshBasicMaterial({ color, transparent: true }));
    m.position.copy(at);
    scene.add(m);
    effects.push({ obj: m, life: 0.5 + Math.random() * 0.3, t: 0, kind: 'spark',
      vel: new THREE.Vector3(Math.random() - 0.5, Math.random() * 0.8, Math.random() - 0.5).multiplyScalar(2.2 * s / 0.25) });
  }
}

function updateEffects(dt) {
  for (let i = effects.length - 1; i >= 0; i--) {
    const e = effects[i];
    e.t += dt;
    const k = 1 - e.t / e.life;
    if (k <= 0) {
      scene.remove(e.obj);
      e.obj.geometry && e.obj.geometry.dispose();
      effects.splice(i, 1);
      continue;
    }
    if (e.kind === 'flash') { e.obj.material.opacity = k; e.obj.scale.setScalar(1 + (1 - k) * 1.5); }
    if (e.kind === 'light') e.obj.intensity = 6 * k;
    if (e.kind === 'fade') e.obj.material.opacity = k;
    if (e.kind === 'spark' || e.kind === 'shell') {
      e.vel.y -= 9.8 * dt * (e.kind === 'shell' ? 0.6 : 0.3);
      e.obj.position.addScaledVector(e.vel, dt);
      if (e.kind === 'shell') { e.obj.rotation.x += e.spin * dt; e.obj.rotation.z += e.spin * 0.7 * dt; }
      if (e.obj.material.transparent) e.obj.material.opacity = k;
    }
  }
  const t = performance.now() / 1000;
  targets.forEach((tg, i) => {
    tg.mesh.position.y = tg.base.y + 0.03 * Math.sin(t * 1.6 + i);
    tg.mesh.lookAt(viewerPosition());
  });
}

function makeTargetMesh(size) {
  const g = new THREE.Group();
  [[1, 0xffffff], [0.72, 0xef4444], [0.46, 0xffffff], [0.22, 0xef4444]].forEach(([r, c], i) => {
    const m = new THREE.Mesh(new THREE.CircleGeometry(size * r, 32), new THREE.MeshBasicMaterial({ color: c, side: THREE.DoubleSide }));
    m.position.z = i * 0.001;
    g.add(m);
  });
  return g;
}

function spawnTarget() {
  if (!targetsOn || targets.length >= 4) return;
  const size = Math.max(heightMetres(), 0.12) * 0.35;
  const mesh = makeTargetMesh(size);
  let pos;
  if (mode === 'room') {
    const c = activeCamera();
    const cp = c.getWorldPosition(new THREE.Vector3());
    const fwd = new THREE.Vector3(0, 0, -1).applyQuaternion(c.getWorldQuaternion(new THREE.Quaternion()));
    fwd.y = 0; fwd.normalize();
    const side = new THREE.Vector3(0, 1, 0).cross(fwd);
    pos = cp.clone().addScaledVector(fwd, 1.5 + Math.random() * 1.5).addScaledVector(side, (Math.random() - 0.5) * 2);
    pos.y = cp.y + (Math.random() - 0.4) * 0.6;
  } else if (mode === 'hand') {
    pos = new THREE.Vector3((Math.random() - 0.5) * 0.8, (Math.random() - 0.3) * 0.4, -1.4 - Math.random() * 0.8);
  } else {
    // In view, beyond the model, spread across the screen.
    const fwd = camera.getWorldDirection(new THREE.Vector3());
    const right = new THREE.Vector3().crossVectors(fwd, camera.up).normalize();
    const up = new THREE.Vector3().crossVectors(right, fwd).normalize();
    const d = camera.position.distanceTo(root.position) + 0.5 + Math.random() * 0.9;
    const half = d * Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
    pos = camera.position.clone().addScaledVector(fwd, d)
      .addScaledVector(right, (Math.random() - 0.5) * 1.4 * half * camera.aspect)
      .addScaledVector(up, (Math.random() - 0.2) * 0.8 * half);
  }
  mesh.position.copy(pos);
  scene.add(mesh);
  targets.push({ mesh, base: pos.clone() });
  sfx.pop();
}
function toggleTargets(on = !targetsOn) {
  targetsOn = on;
  $('btnTargets') && $('btnTargets').classList.toggle('is-on', on);
  if (on) { for (let i = 0; i < 3; i++) setTimeout(spawnTarget, i * 250); hint('Shoot the targets!', 1800); }
  else { targets.splice(0).forEach(tg => scene.remove(tg.mesh)); }
}

// ── Hand tracking (MediaPipe) ────────────────────────────────────────────
let handLandmarker = null;
let camStream = null;
let facing = 'user';
const handHold = { active: false, offset: new THREE.Vector3(), startRoll: 0, startYaw: 0, scale0: 1, twoDist: 0, twoAng: 0 };
const handState = [{}, {}];
let lastWave = 0;
const wristHistory = [];
const video = $('camFeed');
const hctx = $('handCanvas').getContext('2d');

async function startHands() {
  hint('Starting camera + hand tracking…');
  try {
    if (!handLandmarker) {
      const { FilesetResolver, HandLandmarker } = await import('./vendor/mediapipe/vision_bundle.mjs');
      const fileset = await FilesetResolver.forVisionTasks(`${API}/vendor/mediapipe/wasm`);
      handLandmarker = await HandLandmarker.createFromOptions(fileset, {
        baseOptions: { modelAssetPath: `${API}/vendor/mediapipe/hand_landmarker.task`, delegate: 'GPU' },
        runningMode: 'VIDEO', numHands: 2, minHandDetectionConfidence: 0.5, minTrackingConfidence: 0.5,
      });
    }
    await startCamera();
  } catch (err) {
    hint('');
    toast(`Hand tracking unavailable: ${err.message}`, true);
    setMode('3d');
    return;
  }
  hint(role === 'gun'
    ? 'Show your hand: it holds the gun. Curl your index finger to shoot ✊ = reload'
    : 'Pinch (thumb + index) to grab · two hands to resize/turn · poke it · wave hello 👋', 7000);
}

async function startCamera() {
  if (camStream) camStream.getTracks().forEach(t => t.stop());
  camStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: facing, width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false });
  video.srcObject = camStream;
  await video.play();
  video.classList.toggle('mirror', facing === 'user');
  video.style.display = 'block';
  $('handCanvas').style.display = 'block';
}
function stopHands() {
  if (camStream) camStream.getTracks().forEach(t => t.stop());
  camStream = null;
  video.style.display = 'none';
  $('handCanvas').style.display = 'none';
  handHold.active = false;
}

// Landmark (normalised video coords) → screen pixels, honouring object-fit: cover and mirroring.
function lmToScreen(lm) {
  const vw = video.videoWidth || 1280, vh = video.videoHeight || 720;
  const W = window.innerWidth, H = window.innerHeight;
  const s = Math.max(W / vw, H / vh);
  const ox = (W - vw * s) / 2, oy = (H - vh * s) / 2;
  let x = lm.x * vw * s + ox;
  if (facing === 'user') x = W - x;
  return { x, y: lm.y * vh * s + oy };
}
// Screen point + depth (m) → world point for the hand-mode camera.
function screenToWorld(x, y, depth) {
  const v = new THREE.Vector3(ndcFrom(x, y).x, ndcFrom(x, y).y, 0.5).unproject(camera).sub(camera.position).normalize();
  const fwd = camera.getWorldDirection(new THREE.Vector3());
  return camera.position.clone().addScaledVector(v, depth / v.dot(fwd));
}
function handDepth(lms) {
  const a = lmToScreen(lms[0]), b = lmToScreen(lms[9]);
  const palmPx = Math.hypot(a.x - b.x, a.y - b.y);
  const focal = (window.innerHeight / 2) / Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
  return THREE.MathUtils.clamp((0.085 * focal) / Math.max(palmPx, 1), 0.25, 1.6);
}
const dist2 = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);

function analyseHand(lms) {
  const P = lms.map(lmToScreen);
  const palm = dist2(P[0], P[9]);
  const pinch = dist2(P[4], P[8]) / palm;
  const ext = (tip, pip) => dist2(P[tip], P[0]) > dist2(P[pip], P[0]) * 1.12;
  const fingers = [ext(8, 6), ext(12, 10), ext(16, 14), ext(20, 18)];
  const curlIndex = dist2(P[8], P[5]) / palm;
  return {
    P, palm, pinch, fingers,
    open: fingers.every(Boolean),
    fist: fingers.every(f => !f),
    pointing: fingers[0] && !fingers[1] && !fingers[2],
    curlIndex,
    roll: Math.atan2(P[17].y - P[5].y, P[17].x - P[5].x),
    pinchPt: { x: (P[4].x + P[8].x) / 2, y: (P[4].y + P[8].y) / 2 },
    depth: handDepth(lms),
  };
}

function modelScreenBox() {
  const box = new THREE.Box3().setFromObject(anim);
  const pts = [];
  for (const x of [box.min.x, box.max.x]) for (const y of [box.min.y, box.max.y]) for (const z of [box.min.z, box.max.z]) {
    const v = new THREE.Vector3(x, y, z).project(camera);
    pts.push({ x: (v.x + 1) / 2 * window.innerWidth, y: (1 - v.y) / 2 * window.innerHeight });
  }
  return { x0: Math.min(...pts.map(p => p.x)), x1: Math.max(...pts.map(p => p.x)), y0: Math.min(...pts.map(p => p.y)), y1: Math.max(...pts.map(p => p.y)) };
}
const inBox = (p, b, pad = 30) => p.x > b.x0 - pad && p.x < b.x1 + pad && p.y > b.y0 - pad && p.y < b.y1 + pad;

let gunHand = null;
function positionGunInHand() {
  if (!gunHand) return;
  const h = gunHand;
  const grip3 = screenToWorld((h.P[0].x + h.P[9].x) / 2, (h.P[0].y + h.P[9].y) / 2, h.depth);
  const tip3 = screenToWorld(h.P[8].x * 0.5 + h.P[5].x * 0.5, h.P[8].y * 0.5 + h.P[5].y * 0.5, h.depth - 0.03);
  const wrist3 = screenToWorld(h.P[0].x, h.P[0].y, h.depth + 0.02);
  // Aim: from wrist through the index knuckle, pushed forward into the scene.
  const fwd = tip3.clone().sub(wrist3).normalize().add(camera.getWorldDirection(new THREE.Vector3()).multiplyScalar(0.9)).normalize();
  const up = new THREE.Vector3(0, 1, 0);
  const right = new THREE.Vector3().crossVectors(fwd, up).normalize();
  up.crossVectors(right, fwd).normalize();
  const target = new THREE.Matrix4().makeBasis(right, up, fwd.clone().negate());
  const ld = gun.dir.clone().normalize();
  const lr = new THREE.Vector3().crossVectors(ld, new THREE.Vector3(0, 1, 0)).normalize();
  const lu = new THREE.Vector3().crossVectors(lr, ld).normalize();
  const local = new THREE.Matrix4().makeBasis(lr, lu, ld.clone().negate());
  const q = new THREE.Quaternion().setFromRotationMatrix(target.multiply(local.invert()));
  turn.quaternion.copy(q);
  root.rotation.set(0, 0, 0);
  root.updateMatrixWorld(true);
  const gripWorld = gun.grip.clone().applyMatrix4(anim.matrixWorld);
  root.position.add(grip3.clone().sub(gripWorld).multiplyScalar(0.6));
}

function onHands(result) {
  const W = window.innerWidth, H = window.innerHeight;
  const hc = $('handCanvas');
  if (hc.width !== W) { hc.width = W; hc.height = H; }
  hctx.clearRect(0, 0, W, H);
  const hands = (result.landmarks || []).map(analyseHand);

  // Draw a subtle skeleton so you can see what's tracked.
  const bones = [[0,1],[1,2],[2,3],[3,4],[0,5],[5,6],[6,7],[7,8],[5,9],[9,10],[10,11],[11,12],[9,13],[13,14],[14,15],[15,16],[13,17],[17,18],[18,19],[19,20],[0,17]];
  hands.forEach(h => {
    hctx.strokeStyle = 'rgba(124,92,255,0.75)'; hctx.lineWidth = 3;
    bones.forEach(([a, b]) => { hctx.beginPath(); hctx.moveTo(h.P[a].x, h.P[a].y); hctx.lineTo(h.P[b].x, h.P[b].y); hctx.stroke(); });
    h.P.forEach((p, i) => { hctx.fillStyle = [4, 8].includes(i) ? '#22d3ee' : '#fff'; hctx.beginPath(); hctx.arc(p.x, p.y, [4, 8].includes(i) ? 6 : 3.5, 0, 7); hctx.fill(); });
  });

  if (!hands.length) {
    if (handHold.active) releaseHand();   // let go → it drops
    gunHand = null;
    return;
  }
  const box = modelScreenBox();

  // ── Gun: hold in hand, curl index to fire, fist to reload ──
  if (role === 'gun') {
    gunHand = hands[0];
    handHold.active = true;
    phys.grabbed = true;
    phys.airborne = false;
    const h = hands[0];
    const st = handState[0];
    const curled = h.curlIndex < 0.62;
    if (curled && !st.curled) fire();
    st.curled = curled;
    if (h.fist && !st.fist && ammo < AMMO_MAX) reload();
    st.fist = h.fist;
    return;
  }

  // ── Two hands pinching: resize + turn ──
  const pinching = hands.filter(h => h.pinch < 0.38);
  if (pinching.length === 2 && handHold.active) {
    const [a, b] = pinching;
    const d = dist2(a.pinchPt, b.pinchPt);
    const ang = Math.atan2(b.pinchPt.y - a.pinchPt.y, b.pinchPt.x - a.pinchPt.x);
    if (!handHold.two) { handHold.two = true; handHold.twoDist = d; handHold.twoAng = ang; handHold.scale0 = userScale; handHold.yaw0 = root.rotation.y; }
    userScale = THREE.MathUtils.clamp(handHold.scale0 * d / handHold.twoDist, 0.2, 6);
    root.rotation.y = handHold.yaw0 + (ang - handHold.twoAng) * 1.5;
    applyScale();
    return;
  }
  handHold.two = false;

  const h = hands[0];
  const st = handState[0];
  const pinchOn = st.pinch ? h.pinch < 0.5 : h.pinch < 0.33;   // hysteresis
  const p3 = screenToWorld(h.pinchPt.x, h.pinchPt.y, h.depth);
  if (pinchOn && !st.pinch && inBox(h.pinchPt, box, 50)) {
    handHold.active = true;
    handHold.offset.copy(root.position).sub(p3);
    handHold.startRoll = h.roll;
    handHold.startYaw = root.rotation.y;
    handHold.hist = [];
    phys.grabbed = true; phys.airborne = false; phys.vel.set(0, 0, 0);
    if (role === 'character') sfx.boing();
  }
  if (handHold.active && pinchOn) {
    const target = p3.clone().add(handHold.offset);
    root.position.lerp(target, 0.5);
    root.rotation.y = handHold.startYaw - (h.roll - handHold.startRoll);
    handHold.hist.push({ p: root.position.clone(), t: performance.now() });
    if (handHold.hist.length > 6) handHold.hist.shift();
  }
  if (!pinchOn && st.pinch && handHold.active) releaseHand();
  st.pinch = pinchOn;

  // Poke with an index finger
  if (h.pointing && !handHold.active) {
    const touching = inBox(h.P[8], box, 0);
    if (touching && !st.poke) { play(role === 'character' ? 'wobble' : 'tumble'); sfx.boing(); }
    st.poke = touching;
  }

  // Wave hello (open palm moving side to side)
  if (h.open) {
    const now = performance.now();
    wristHistory.push({ x: h.P[0].x, t: now });
    while (wristHistory.length && now - wristHistory[0].t > 1200) wristHistory.shift();
    const xs = wristHistory.map(w => w.x);
    let turns = 0;
    for (let i = 2; i < xs.length; i++) if ((xs[i] - xs[i - 1]) * (xs[i - 1] - xs[i - 2]) < 0) turns++;
    if (turns >= 3 && Math.max(...xs) - Math.min(...xs) > 60 && now - lastWave > 4000) {
      lastWave = now;
      wristHistory.length = 0;
      play('wave');
      if (role === 'character') sayLocal(['Hi there!', 'Hello! Nice to see you!', 'Hey! Wanna play?'][Math.floor(Math.random() * 3)]);
    }
  } else wristHistory.length = 0;
}

function releaseHand() {
  handHold.active = false;
  phys.grabbed = false;
  const hist = handHold.hist || [];
  if (hist.length >= 2) {
    const a = hist[0], b = hist[hist.length - 1];
    phys.vel.copy(b.p).sub(a.p).multiplyScalar(1000 / Math.max(b.t - a.t, 16)).clampLength(0, 3);
    phys.spin = (Math.random() - 0.5) * 5 * Math.min(phys.vel.length(), 1);
  }
  phys.airborne = true;
}

// ── Room AR (WebXR) ──────────────────────────────────────────────────────
let xrSession = null, hitTestSource = null, reticleHit = null, gestureMoved = false, lastTouch = null, pinchGesture = null;

async function startRoom() {
  try {
    xrSession = await navigator.xr.requestSession('immersive-ar', {
      requiredFeatures: ['hit-test'], optionalFeatures: ['dom-overlay'], domOverlay: { root: $('overlay') },
    });
  } catch (err) {
    toast(`Couldn't start AR: ${err.message}`, true);
    setMode('3d');
    return;
  }
  placed = false;
  floor.visible = false;
  controls.enabled = false;
  scene.background = null;
  root.visible = false;
  floorY = 0;
  $('overlay').classList.add('xr');
  renderer.xr.enabled = true;
  renderer.xr.setReferenceSpaceType('local');
  await renderer.xr.setSession(xrSession);
  hitTestSource = await xrSession.requestHitTestSource({ space: await xrSession.requestReferenceSpace('viewer') });
  xrSession.addEventListener('select', onXRSelect);
  xrSession.addEventListener('end', () => {
    xrSession = null; hitTestSource = null; reticle.visible = false; renderer.xr.enabled = false;
    $('overlay').classList.remove('xr');
    $('crosshair').style.display = 'none';
    if (mode === 'room') setMode('3d');
  });
  hint('Move your phone slowly to find the floor…');
}

function onXRSelect() {
  if (gestureMoved) return;
  ac();
  if (!placed) {
    if (!reticleHit) return;
    root.position.copy(reticleHit);
    floorY = reticleHit.y;
    root.visible = true; placed = true;
    faceViewer();
    if (role === 'gun') {
      phys.grabbed = true;
      $('crosshair').style.display = 'block';
      hint('Tap anywhere to shoot · turn on Targets to play', 5000);
    } else {
      play('jump');
      hint('Tap a spot → it walks there · tap it → it reacts · pinch → size · twist → turn', 6000);
    }
    return;
  }
  if (role === 'gun') { fire(); return; }
  if (lastTouch && hitsModel(lastTouch.x, lastTouch.y)) { play(role === 'character' ? 'happy' : 'tumble'); return; }
  if (reticleHit) { following = false; $('btnFollow') && $('btnFollow').classList.remove('is-on'); walkTarget = reticleHit.clone(); }
}

function touchInfo(t) { return { d: Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY), ang: Math.atan2(t[1].clientY - t[0].clientY, t[1].clientX - t[0].clientX), my: (t[0].clientY + t[1].clientY) / 2 }; }
$('overlay').addEventListener('touchstart', (e) => {
  if (mode !== 'room' || e.target.closest('.ui')) return;
  gestureMoved = false;
  lastTouch = { x: e.touches[0].clientX, y: e.touches[0].clientY, sx: e.touches[0].clientX, sy: e.touches[0].clientY };
  if (e.touches.length === 2) { const i = touchInfo(e.touches); pinchGesture = { ...i, scale: userScale, q: turn.quaternion.clone() }; }
}, { passive: true });
$('overlay').addEventListener('touchmove', (e) => {
  if (mode !== 'room' || !placed || e.target.closest('.ui')) return;
  if (e.touches.length === 2 && pinchGesture) {
    const i = touchInfo(e.touches);
    userScale = THREE.MathUtils.clamp(pinchGesture.scale * i.d / pinchGesture.d, 0.15, 6);
    // twist = spin around up; two-finger vertical drag = tilt (full 360° possible)
    const spin = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 1, 0), -(i.ang - pinchGesture.ang));
    const tilt = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1, 0, 0), (i.my - pinchGesture.my) * 0.012);
    turn.quaternion.copy(pinchGesture.q).premultiply(spin).premultiply(tilt);
    applyScale();
    gestureMoved = true;
  } else if (e.touches.length === 1 && lastTouch) {
    const dx = e.touches[0].clientX - lastTouch.x;
    if (Math.abs(e.touches[0].clientX - lastTouch.sx) > 14) {
      turn.rotateOnWorldAxis(new THREE.Vector3(0, 1, 0), dx * 0.012);   // unlimited 360° spin
      gestureMoved = true;
    }
    lastTouch = { ...lastTouch, x: e.touches[0].clientX, y: e.touches[0].clientY };
  }
}, { passive: true });
$('overlay').addEventListener('touchend', (e) => {
  if (e.touches.length < 2) pinchGesture = null;
  const t = e.changedTouches[0];
  if (t && !gestureMoved) lastTouch = { x: t.clientX, y: t.clientY, sx: t.clientX, sy: t.clientY };
}, { passive: true });
document.querySelectorAll('#overlay .ui').forEach(el => el.addEventListener('beforexrselect', e => e.preventDefault()));

// ── iPhone Quick Look ────────────────────────────────────────────────────
async function setupQuickLook() {
  const sel = $('qlAnim');
  if (!sel.options.length) {
    const { animations } = await (await fetch(`${API}/ar/presets`)).json();
    sel.innerHTML = animations.map(a => `<option value="${a}">${a === 'none' ? 'Still' : a[0].toUpperCase() + a.slice(1)}</option>`).join('');
    sel.value = role === 'character' ? 'wave' : 'idle';
    sel.addEventListener('change', refreshQL);
  }
  await refreshQL();
}
async function refreshQL() {
  const link = $('quickLook');
  link.classList.add('hidden');
  $('qlStatus').textContent = 'Preparing AR file…';
  try {
    const res = await postJSON(`/scans/${encodeURIComponent(scanId)}/ar`, { animation: $('qlAnim').value, height_cm: Math.round(heightMetres() * 100) });
    const ar = await res.json();
    link.href = `${API}${ar.usdz_url}#allowsContentScaling=1`;
    link.classList.remove('hidden');
    $('qlStatus').textContent = 'Drag to move · pinch to resize · twist to rotate';
  } catch (err) {
    $('qlStatus').textContent = `AR file failed: ${err.message}`;
  }
}

// ── AI chat + voice ──────────────────────────────────────────────────────
function addMsg(kind, text) {
  const el = document.createElement('div');
  el.className = `msg ${kind}`;
  el.textContent = text;
  $('chatLog').appendChild(el);
  $('chatLog').scrollTop = 1e9;
  while ($('chatLog').children.length > 30) $('chatLog').firstChild.remove();
  return el;
}

let speechTimer = null;
function showSpeech(text) {
  const el = $('speech');
  el.textContent = text;
  el.classList.add('show');
  clearTimeout(speechTimer);
  speechTimer = setTimeout(() => el.classList.remove('show'), Math.max(3000, text.length * 75));
}
function updateSpeechBubble() {
  const el = $('speech');
  if (!el.classList.contains('show')) return;
  const head = new THREE.Vector3(0, modelHeight * 1.05, 0).applyMatrix4(anim.matrixWorld);
  const v = head.project(activeCamera());
  el.style.left = `${(v.x + 1) / 2 * window.innerWidth}px`;
  el.style.top = `${Math.max((1 - v.y) / 2 * window.innerHeight - 8, 90)}px`;
}

async function speakText(text) {
  try {
    const voice = (scan.ai && scan.ai.voice) || 'cute';
    const res = await postJSON('/ai/tts', { text, voice });
    const buf = await (await res.blob()).arrayBuffer();
    const a = ac();
    const audio = await a.decodeAudioData(buf);
    const src = a.createBufferSource();
    src.buffer = audio;
    const an = a.createAnalyser();
    an.fftSize = 512;
    src.connect(an); an.connect(a.destination);
    talking = { an, data: new Uint8Array(an.fftSize) };
    src.onended = () => { talking = null; };
    src.start();
  } catch (err) {
    console.warn('TTS failed', err);
  }
}
function sayLocal(text) { showSpeech(text); speakText(text); }

async function sendChat(text) {
  text = text.trim();
  if (!text || busyChat) return;
  busyChat = true;
  addMsg('user', text);
  chatHistory.push({ role: 'user', content: text });
  const thinking = addMsg('sys', `${$('title').textContent} is thinking…`);
  play('think');
  try {
    const r = await (await postJSON(`/scans/${encodeURIComponent(scanId)}/chat`, { message: text, history: chatHistory })).json();
    thinking.remove();
    chatHistory.push({ role: 'assistant', content: r.reply });
    addMsg('bot', r.reply);
    showSpeech(r.reply);
    if (r.action && r.action !== 'none') play(r.action);
    speakText(r.reply);
  } catch (err) {
    thinking.textContent = err.message;
  } finally {
    busyChat = false;
  }
}

let recorder = null, recChunks = [], recTimer = null;
async function toggleMic() {
  ac();
  const btn = $('micBtn');
  if (recorder && recorder.state === 'recording') { recorder.stop(); return; }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    const type = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg'].find(t => window.MediaRecorder && MediaRecorder.isTypeSupported(t)) || '';
    recorder = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
    recChunks = [];
    recorder.ondataavailable = e => e.data.size && recChunks.push(e.data);
    recorder.onstop = async () => {
      clearTimeout(recTimer);
      stream.getTracks().forEach(t => t.stop());
      btn.classList.remove('rec');
      btn.classList.add('busy');
      try {
        const fd = new FormData();
        fd.append('audio', new Blob(recChunks, { type: recorder.mimeType || 'audio/webm' }), 'speech');
        const res = await fetch(`${API}/ai/stt`, { method: 'POST', body: fd });
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
        const { text } = await res.json();
        if (text) sendChat(text); else toast("Didn't catch that. Try again?");
      } catch (err) {
        toast(`Voice failed: ${err.message}`, true);
      } finally {
        btn.classList.remove('busy');
      }
    };
    recorder.start();
    btn.classList.add('rec');
    hint('Listening… tap the mic again when you\'re done', 3000);
    recTimer = setTimeout(() => recorder.state === 'recording' && recorder.stop(), 15000);
  } catch (err) {
    toast(`Microphone unavailable: ${err.message}`, true);
  }
}

// ── Role-specific dock ───────────────────────────────────────────────────
const DOCKS = {
  character: [['wave', '👋', 'Wave'], ['dance', '💃', 'Dance'], ['jump', '⤴', 'Jump'], ['backflip', '🤸', 'Flip'],
    ['celebrate', '🎉', 'Party'], ['nod', '🙂', 'Nod'], ['shake', '🙅', 'No'], ['sleep', '😴', 'Sleep'], ['spin', '🌀', 'Spin'], ['follow', '🚶', 'Follow']],
  gun: [['fire', '💥', 'Fire'], ['reload', '🔄', 'Reload'], ['targets', '🎯', 'Targets'], ['flip', '⇄', 'Flip barrel'], ['spin', '🌀', 'Twirl']],
  object: [['tumble', '🎲', 'Tumble'], ['bounce', '⚽', 'Bounce'], ['roll', '🛞', 'Roll'], ['spin', '🌀', 'Spin'], ['jump', '⤴', 'Hop'], ['throw', '🫳', 'Toss'], ['follow', '🚶', 'Follow']],
};
function buildDock() {
  const dock = $('dock');
  dock.innerHTML = '';
  DOCKS[role].forEach(([act, icon, label]) => {
    const b = document.createElement('button');
    b.className = 'tool';
    b.type = 'button';
    b.id = `btn${act[0].toUpperCase()}${act.slice(1)}`;
    b.innerHTML = `<span>${icon}</span>${label}`;
    b.addEventListener('click', () => {
      ac();
      try { navigator.vibrate && navigator.vibrate(8); } catch (_) { /* ignore */ }
      if (act === 'fire') fire();
      else if (act === 'reload') reload();
      else if (act === 'targets') toggleTargets();
      else if (act === 'flip') flipBarrel();
      else if (act === 'follow') { following = !following; b.classList.toggle('is-on', following); if (!following) walkTarget = null; }
      else if (act === 'throw') { phys.vel.set((Math.random() - 0.5) * 0.6, 2.2 * Math.max(heightMetres() / 0.3, 0.5), (Math.random() - 0.5) * 0.6); phys.spin = 6; phys.airborne = true; sfx.whoosh(); }
      else play(act);
    });
    b.addEventListener('beforexrselect', e => e.preventDefault());
    dock.appendChild(b);
  });
  show('chat', role === 'character');
  show('gunHud', role === 'gun');
  document.querySelectorAll('#roleSeg button').forEach(x => x.classList.toggle('is-active', x.dataset.role === role));
  if (role !== 'gun') toggleTargets(false);
}

async function setRole(r) {
  if (r === role) return;
  role = r;
  setBaseScale();
  phys.grabbed = false;
  if (mode === '3d') root.position.set(0, role === 'gun' ? 0.14 : 0, 0);
  if (role !== 'gun') { turn.quaternion.identity(); }
  buildDock();
  try { await postJSON(`/scans/${encodeURIComponent(scanId)}/role`, { role }); } catch (_) { /* local only */ }
  if (role === 'character' && !chatHistory.length) greet();
}

function greet() {
  if (!$('chatLog').children.length) {
    const name = (scan.ai && scan.ai.name) || 'your scan';
    addMsg('sys', `Talk to ${name}: tap the mic or type.`);
  }
}

// ── Modes ────────────────────────────────────────────────────────────────
function setMode(m) {
  const prev = mode;
  mode = m;
  document.querySelectorAll('#modeSeg button').forEach(b => b.classList.toggle('is-active', b.dataset.mode === m));
  show('qlSheet', m === 'quicklook');
  show('leftDock', m !== 'quicklook');
  show('dock', m !== 'quicklook');
  show('bottom', m !== 'quicklook');
  if (prev === 'hand' && m !== 'hand') stopHands();
  if (prev === 'room' && m !== 'room' && xrSession) xrSession.end();
  $('crosshair').style.display = 'none';

  if (m === '3d' || m === 'quicklook') {
    scene.background = new THREE.Color(0x0a0a0e);
    floor.visible = true;
    controls.enabled = true;
    floorY = 0;
    placed = true;
    root.visible = true;
    camera.position.set(0.55, 0.42, 0.85);
    controls.target.set(0, 0.14, 0);
    if (prev === 'hand' || prev === 'room') { root.position.set(0, 0, 0); turn.quaternion.identity(); }
    if (role === 'gun') { root.position.set(0, 0.14, 0); phys.grabbed = true; }
    hint(role === 'gun' ? 'Click or press Space to fire · drag the gun to aim it' : 'Drag the model to spin it 360° · drag the background to orbit · click the floor to walk', 6000);
    if (m === 'quicklook') setupQuickLook();
  } else if (m === 'hand') {
    scene.background = null;
    floor.visible = false;
    controls.enabled = false;
    camera.position.set(0, 0, 0);
    camera.lookAt(0, 0, -1);
    floorY = -0.22;
    root.position.set(0, floorY, -0.75);
    root.rotation.set(0, 0, 0);
    turn.quaternion.identity();
    placed = true;
    root.visible = true;
    startHands();
  } else if (m === 'room') {
    startRoom();
  }
}

// ── Buttons ──────────────────────────────────────────────────────────────
function setTool(t) {
  tool = t;
  $('toolRotate').classList.toggle('is-on', t === 'rotate');
  $('toolMove').classList.toggle('is-on', t === 'move');
}
$('toolRotate').addEventListener('click', () => setTool('rotate'));
$('toolMove').addEventListener('click', () => setTool('move'));
$('tool360').addEventListener('click', () => play('spin360'));
$('toolTurntable').addEventListener('click', () => { turntable = !turntable; $('toolTurntable').classList.toggle('is-on', turntable); });
$('toolUpright').addEventListener('click', () => {
  turn.quaternion.identity(); phys.vel.set(0, 0, 0); phys.airborne = false;
  if (mode === '3d') { root.position.set(0, 0, 0); faceViewer(); }
});
$('toolBigger').addEventListener('click', () => { userScale = Math.min(userScale * 1.25, 6); applyScale(); });
$('toolSmaller').addEventListener('click', () => { userScale = Math.max(userScale / 1.25, 0.15); applyScale(); });
document.querySelectorAll('#modeSeg button').forEach(b => b.addEventListener('click', () => setMode(b.dataset.mode)));
document.querySelectorAll('#roleSeg button').forEach(b => b.addEventListener('click', () => setRole(b.dataset.role)));
$('fireBtn').addEventListener('click', () => { ac(); fire(); });
$('chatForm').addEventListener('submit', (e) => { e.preventDefault(); const v = $('chatInput').value; $('chatInput').value = ''; sendChat(v); });
$('micBtn').addEventListener('click', toggleMic);
$('backBtn').href = scanId ? `/?scan=${encodeURIComponent(scanId)}` : '/';
window.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT') return;
  if (e.code === 'Space' && role === 'gun') { e.preventDefault(); ac(); fire(); }
  if (e.key === 'r' && role === 'gun') reload();
  if (e.key === 'ArrowLeft') turn.rotateOnWorldAxis(new THREE.Vector3(0, 1, 0), -0.2);
  if (e.key === 'ArrowRight') turn.rotateOnWorldAxis(new THREE.Vector3(0, 1, 0), 0.2);
  if (e.key === 'ArrowUp') turn.rotateOnWorldAxis(new THREE.Vector3(1, 0, 0), -0.2);
  if (e.key === 'ArrowDown') turn.rotateOnWorldAxis(new THREE.Vector3(1, 0, 0), 0.2);
});

// ── Render loop ──────────────────────────────────────────────────────────
const clock = new THREE.Clock();
let lastVideoTime = -1;
renderer.setAnimationLoop((time, frame) => {
  const dt = Math.min(clock.getDelta(), 0.05);
  const t = performance.now() / 1000;
  if (frame && hitTestSource) {
    const res = frame.getHitTestResults(hitTestSource);
    if (res.length) {
      const pose = res[0].getPose(renderer.xr.getReferenceSpace());
      reticle.visible = role !== 'gun' || !placed;
      reticle.matrix.fromArray(pose.transform.matrix);
      reticleHit = new THREE.Vector3().setFromMatrixPosition(reticle.matrix);
      if (!placed) hint('Tap to place it here');
    } else { reticle.visible = false; reticleHit = null; }
    // In room AR the gun rides in front of your phone like a first-person view.
    if (role === 'gun' && placed) {
      const c = activeCamera();
      c.updateMatrixWorld();
      const offset = new THREE.Vector3(0.09, -0.11, -0.32).applyMatrix4(c.matrixWorld);
      root.position.lerp(offset, 0.5);
      const fwd = new THREE.Vector3(0, 0, -1).applyQuaternion(c.getWorldQuaternion(new THREE.Quaternion()));
      const q = new THREE.Quaternion().setFromUnitVectors(gun.dir.clone().normalize(), fwd);
      root.rotation.set(0, 0, 0);
      turn.quaternion.slerp(q, 0.5);
    }
  }
  if (mode === 'hand' && handLandmarker && video.readyState >= 2 && video.currentTime !== lastVideoTime) {
    lastVideoTime = video.currentTime;
    try { onHands(handLandmarker.detectForVideo(video, performance.now())); } catch (_) { /* skip frame */ }
  }
  if (mode === '3d' && role === 'gun') { phys.grabbed = !drag || tool === 'rotate'; if (!drag && !action) aimGun(); }
  if (mode !== 'room' && mode !== 'hand') controls.update();
  update(dt, t);
  if (targetsOn && targets.length < 3 && Math.random() < 0.01) spawnTarget();
  renderer.render(scene, camera);
});

window.addEventListener('resize', () => {
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
});

// ── Start ────────────────────────────────────────────────────────────────
(async () => {
  try {
    await loadScan();
    $('loading').classList.add('hidden');
    const xrOK = navigator.xr && await navigator.xr.isSessionSupported('immersive-ar').catch(() => false);
    const qlOK = document.createElement('a').relList.supports?.('ar');
    $('roomModeBtn').disabled = !xrOK;
    $('roomModeBtn').title = xrOK ? '' : 'Room AR needs Chrome on an ARCore Android phone';
    $('qlModeBtn').classList.toggle('hidden', !qlOK);
    setTool('rotate');
    buildDock();
    setMode(params.get('mode') || '3d');
    faceViewer();
    if (role === 'character') greet();
    if (!scan.ai) {
      fetch(`${API}/scans/${encodeURIComponent(scanId)}/identify`, { method: 'POST' })
        .then(r => r.ok ? r.json() : null)
        .then(s => {
          if (!s || !s.ai) return;
          scan = s;
          $('title').textContent = s.ai.name || s.name;
          $('subtitle').textContent = s.ai.what || '';
          if (s.ai.role && s.ai.role !== role) { setRole(s.ai.role); toast(`Looks like a ${s.ai.what || s.ai.role}: switched to ${s.ai.role} mode`); }
        }).catch(() => {});
    }
  } catch (err) {
    $('loadingText').textContent = err.message;
    document.querySelector('#loading .spinner').style.display = 'none';
  }
})();

