// Photo → 3D → Blender. Everything runs on this Mac.

const API_BASE = window.location.origin;
const $ = (id) => document.getElementById(id);

// DOM elements
const appContainer = $('appContainer');
const video = $('camera');
const canvas = $('snapshot');
const preview = $('preview');
const previewBg = $('previewBg');
const tapMarkers = $('tapMarkers');
const placeholder = $('placeholder');
const captureBtn = $('capture');
const sendBtn = $('sendToBlender');
const controlsMain = $('controlsMain');
const confirmBtn = $('confirmBtn');
const retakeBtn = $('retakeBtn');
const startAgainBtn = $('startAgainBtn');
const cancelGenerationBtn = $('cancelGenerationBtn');
const generationSegmentsTrack = $('generationSegmentsTrack');
const generationLabel = $('generationLabel');
const dotRippleCanvas = $('dotRippleCanvas');
const segmentsTrack = $('segmentsTrack');
const isolateLabel = $('isolateLabel');
const isolateCancelBtn = $('isolateCancelBtn');
const modelViewerContainer = $('modelViewerContainer');
const modelViewer = $('modelViewer');
const modelStatusBadge = $('modelStatusBadge');
const connectionStatus = $('connectionStatus');
const tooltip = $('tooltip');
const blenderConnectUrl = $('blenderConnectUrl');
const blenderCopyBtn = $('blenderCopyBtn');

let stream = null;
let capturedBlob = null;
let isolateCancelled = false;
let pendingIsolatedBlob = null;
let cameraActive = false;
let activeStreams = [];
let generationCancelled = false;
let facingMode = 'environment';
let generationFakeInterval = null;

let appMode = 'photo';          // photo | multiview | gallery
let currentScan = null;         // public scan object from the server
let relayModelUrl = null;       // what "Send to Blender" sends (/scans-files/...)
let selectPoints = [];          // [[x, y, label], ...] in photo pixels
let selectLabel = 1;            // 1 keep, 0 remove
let selectTimer = null;
let selectSeq = 0;
let phoneUrl = null;
let busyTool = false;

const MV_ORDER = ['front', 'left', 'back', 'right'];
const mvViews = { front: null, left: null, back: null, right: null };
const MV_HINTS = {
  front: '1/4 · FRONT: the object faces you',
  left: '2/4 · Turn it so its front points to your LEFT',
  back: '3/4 · Turn again: its BACK faces you',
  right: '4/4 · Last turn: its front points to your RIGHT',
};

// Processing settings (remembered per browser)
const settings = { engine: 'hunyuan', quality: 'standard', texture: true };
let engineInfo = { hunyuan: true };

function loadSettings() {
  try { Object.assign(settings, JSON.parse(localStorage.getItem('c2b-settings') || '{}')); } catch (_) { /* ignore */ }
}
function saveSettings() {
  try { localStorage.setItem('c2b-settings', JSON.stringify(settings)); } catch (_) { /* ignore */ }
}

function renderSettings() {
  settings.engine = 'hunyuan';
  document.querySelectorAll('[data-quality]').forEach(b => b.classList.toggle('is-active', b.dataset.quality === settings.quality));
  const t = $('textureToggle');
  t.classList.toggle('is-on', settings.texture);
  t.textContent = settings.texture ? 'Texture ✓' : 'Texture ✕';
  const q = settings.quality[0].toUpperCase() + settings.quality.slice(1);
  const label = `Generate 3D · ${q}`;
  $('confirmLabel').textContent = label;
  const eta = ETA[settings.engine][settings.quality];
  $('settingsSummaryText').textContent = `Quality: ${q}${settings.texture ? '' : ' · No texture'}`;
  $('etaNote').textContent = `About ${eta} on your computer`;
  if (appMode === 'multiview') renderMvStrip();
}

const ETA = {
  hunyuan: { draft: '30 s', standard: '45 s', high: '80 s', ultra: '3–4 min' },
};
function haptic(ms = 12) { try { navigator.vibrate && navigator.vibrate(ms); } catch (_) { /* ignore */ } }
function closeSettings() { $('settingsBar').classList.remove('open'); $('settingsSummary').setAttribute('aria-expanded', 'false'); }

function setStage(stage) { appContainer.dataset.stage = stage; if (stage !== 'idle' && stage !== 'review') closeSettings(); }

// ── Toasts ────────────────────────────────────────────────────────────────

let taskToastTimer = null;
function showTaskToast(text, { error = false, sticky = false } = {}) {
  const el = $('taskToast');
  $('taskToastText').textContent = text;
  el.classList.toggle('is-error', error);
  el.style.display = 'block';
  clearTimeout(taskToastTimer);
  if (!sticky) taskToastTimer = setTimeout(() => { el.style.display = 'none'; }, error ? 7000 : 2800);
}
function hideTaskToast() { $('taskToast').style.display = 'none'; }

// ── Control groups ────────────────────────────────────────────────────────

function showPhotoReview(pngBlob) {
  pendingIsolatedBlob = pngBlob;
  controlsMain.classList.add('review-mode');
  setStage('review');
}

function hidePhotoReview() {
  pendingIsolatedBlob = null;
  controlsMain.classList.remove('review-mode');
  clearSelection();
  previewBg.style.display = 'none';
}

function showGenerationControls() {
  controlsMain.classList.add('generation-mode');
  setStage('generating');
  updateGenerationProgress(`Starting · usually ${ETA[settings.engine][settings.quality]}`, 0);
}

function stopGenerationFakeCrawl() {
  if (generationFakeInterval) { clearInterval(generationFakeInterval); generationFakeInterval = null; }
}

function hideGenerationControls() {
  stopGenerationFakeCrawl();
  controlsMain.classList.remove('generation-mode');
}

function showModelReadyControls() {
  controlsMain.classList.add('model-ready-mode');
  sendBtn.disabled = false;
  setStage('ready');
  renderModelInfo();
}

function hideModelReadyControls() {
  controlsMain.classList.remove('model-ready-mode');
  sendBtn.disabled = true;
}

function updateGenerationProgress(text, percentage = 0) {
  if (generationLabel) generationLabel.textContent = text;
  applySegmentStyles(generationSegments, percentage);
}

function closeStreams() {
  activeStreams.forEach(es => { try { es.close(); } catch (_) { /* ignore */ } });
  activeStreams = [];
}

function hideModelViewer() {
  if (window.pointCloudLoader) window.pointCloudLoader.stop();
  modelViewerContainer.style.display = 'none';
  modelViewer.removeAttribute('src');
  modelViewer.style.opacity = '0';
  modelViewer.style.pointerEvents = 'none';
  modelStatusBadge.style.display = 'none';
}

function resetToStart() {
  generationCancelled = true;
  stopGenerationFakeCrawl();
  closeStreams();
  hideTaskToast();
  hidePhotoReview();
  hideGenerationControls();
  hideModelReadyControls();
  hideIsolateLoading();
  hideModelViewer();
  preview.style.display = 'none';
  preview.src = '';
  capturedBlob = null;
  pendingIsolatedBlob = null;
  currentScan = null;
  relayModelUrl = null;
  captureBtn.disabled = false;
  $('modelInfo').textContent = '';
  setStage('idle');
  applyModeView();
}

function confirmPhoto() {
  if (!pendingIsolatedBlob) return;
  const blob = pendingIsolatedBlob;
  hidePhotoReview();
  const fd = new FormData();
  fd.append('image', new Blob([blob], { type: 'image/png' }), 'image.png');
  appendSettings(fd);
  startGeneration(() => postForm('/generate3d', fd), blob);
}

function appendSettings(fd) {
  fd.append('engine', settings.engine);
  fd.append('quality', settings.quality);
  fd.append('texture', settings.texture ? 'true' : 'false');
}

function retakePhoto() {
  hidePhotoReview();
  preview.style.display = 'none';
  preview.src = '';
  capturedBlob = null;
  captureBtn.disabled = false;
  hideModelReadyControls();
  setStage('idle');
  startCamera();
}

// ── Segmented loader ──────────────────────────────────────────────────────

const SEGMENT_COUNT = 20;
const segments = [];
const generationSegments = [];

function applySegmentStyles(segsArray, percentage) {
  const filled = Math.round((percentage / 100) * SEGMENT_COUNT);
  segsArray.forEach((seg, i) => {
    if (i < filled) {
      seg.style.background = 'rgba(255,255,255,1)';
      seg.style.boxShadow = '0 0 6px rgba(255,255,255,0.65), 0 0 14px rgba(255,255,255,0.3)';
    } else {
      const fadeBars = 1 + Math.round((filled / SEGMENT_COUNT) * 7);
      const t = Math.min((i - filled) / fadeBars, 1);
      seg.style.background = `rgba(255,255,255,${(0.30 - 0.22 * t).toFixed(2)})`;
      seg.style.boxShadow = 'none';
    }
  });
}

function initSegmentTrack(track, arr) {
  for (let i = 0; i < SEGMENT_COUNT; i++) {
    const seg = document.createElement('div');
    seg.className = 'seg';
    track.appendChild(seg);
    arr.push(seg);
  }
}

// ── Dot ripple canvas ─────────────────────────────────────────────────────

let dotRippleRAF = null;
function startDotRipple() {
  if (dotRippleRAF) return;
  dotRippleCanvas.style.display = 'block';
  const ctx = dotRippleCanvas.getContext('2d');
  const spacing = 22;
  let frame = 0;
  function draw() {
    const w = dotRippleCanvas.offsetWidth, h = dotRippleCanvas.offsetHeight;
    if (dotRippleCanvas.width !== w) dotRippleCanvas.width = w;
    if (dotRippleCanvas.height !== h) dotRippleCanvas.height = h;
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = 'rgba(0,0,0,0.16)';
    ctx.fillRect(0, 0, w, h);
    const cx = w / 2, cy = h / 2;
    for (let x = spacing / 2; x < w; x += spacing) {
      for (let y = spacing / 2; y < h; y += spacing) {
        const wave = Math.sin(Math.hypot(x - cx, y - cy) / 34 - frame / 33);
        ctx.beginPath();
        ctx.arc(x, y, 1.5, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(255,255,255,${(0.04 + ((wave + 1) / 2) * 0.52).toFixed(2)})`;
        ctx.fill();
      }
    }
    frame++;
    dotRippleRAF = requestAnimationFrame(draw);
  }
  draw();
}
function stopDotRipple() {
  if (dotRippleRAF) { cancelAnimationFrame(dotRippleRAF); dotRippleRAF = null; }
  dotRippleCanvas.style.display = 'none';
}

// ── Isolation loading state ──────────────────────────────────────────────

function showIsolateLoading() {
  isolateCancelled = false;
  controlsMain.classList.add('isolate-mode');
  applySegmentStyles(segments, 0);
}
function hideIsolateLoading() { controlsMain.classList.remove('isolate-mode'); }

function cancelIsolate() {
  isolateCancelled = true;
  hideIsolateLoading();
  stopDotRipple();
  preview.style.display = 'none';
  preview.src = '';
  capturedBlob = null;
  captureBtn.disabled = false;
  setStage('idle');
  startCamera();
}

function updateProgress(text, percentage = 0) {
  if (isolateLabel) isolateLabel.textContent = text;
  applySegmentStyles(segments, percentage);
}

// ── Camera ────────────────────────────────────────────────────────────────

async function startCamera() {
  if (cameraActive || appMode === 'gallery') return;
  if (['ready', 'generating', 'review', 'isolating'].includes(appContainer.dataset.stage)) return;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode, width: { ideal: 1920 }, height: { ideal: 1440 } }, audio: false,
    });
    video.srcObject = stream;
    video.style.display = 'block';
    placeholder.style.display = 'none';
    preview.style.display = 'none';
    cameraActive = true;
    captureBtn.disabled = false;
    hideModelReadyControls();
  } catch (err) {
    console.error('[Camera] Access denied:', err.name, err.message);
    const hint = window.isSecureContext ? '' : '\n\nCameras only work over HTTPS. Use the phone link in the Gallery tab.';
    alert(`Unable to access camera.\nError: ${err.name}\n${err.message}${hint}`);
  }
}

function stopCamera() {
  if (stream) { stream.getTracks().forEach(t => t.stop()); stream = null; }
  video.style.display = 'none';
  cameraActive = false;
}

function snapshotBlob() {
  const ctx = canvas.getContext('2d');
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  ctx.drawImage(video, 0, 0);
  return new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
}

async function capture() {
  if (!cameraActive) { await startCamera(); return; }
  haptic(18);
  closeSettings();
  captureBtn.classList.add('flash');
  setTimeout(() => captureBtn.classList.remove('flash'), 180);
  if (appMode === 'multiview') return captureView();
  const blob = await snapshotBlob();
  stopCamera();
  await reviewPhoto(blob);
}

// Show a photo (camera or uploaded file), auto-cut it out, and open the review step.
async function reviewPhoto(blob) {
  capturedBlob = blob;
  hideModelViewer();
  captureBtn.disabled = true;
  hideModelReadyControls();
  const capturedUrl = URL.createObjectURL(blob);
  preview.src = capturedUrl;
  previewBg.src = capturedUrl;
  preview.style.display = 'block';
  placeholder.style.display = 'none';
  setStage('isolating');
  try {
    showIsolateLoading();
    updateProgress('Isolating object...', 0);
    let fakePct = 0;
    const fakeInterval = setInterval(() => {
      if (isolateCancelled) { clearInterval(fakeInterval); return; }
      fakePct += (85 - fakePct) * 0.04;
      updateProgress('Isolating object...', fakePct);
    }, 80);
    const isolatedBlob = await isolateObject(blob);
    clearInterval(fakeInterval);
    const pngBlob = new Blob([isolatedBlob], { type: 'image/png' });
    updateProgress('Ready!', 100);
    await new Promise(r => setTimeout(r, 300));
    if (isolateCancelled) return;
    hideIsolateLoading();
    preview.src = URL.createObjectURL(pngBlob);
    previewBg.style.display = 'block';
    showPhotoReview(pngBlob);
  } catch (err) {
    hideIsolateLoading();
    alert('Background removal failed. Please try again.');
    captureBtn.disabled = false;
    setStage('idle');
  }
}

// ── Tap-to-select (SAM) ──────────────────────────────────────────────────

function clearSelection() {
  selectPoints = [];
  tapMarkers.innerHTML = '';
  clearTimeout(selectTimer);
  selectSeq++;
  $('selectHint').textContent = 'TAP THE OBJECT TO SELECT IT';
}

function setSelectLabel(label) {
  selectLabel = label;
  $('selKeep').classList.toggle('is-active', label === 1);
  $('selRemove').classList.toggle('is-active', label === 0);
}

// Map a click on an object-fit: cover <img> to natural image pixels.
function clientToImage(img, clientX, clientY) {
  const rect = img.getBoundingClientRect();
  const nw = img.naturalWidth, nh = img.naturalHeight;
  const scale = Math.max(rect.width / nw, rect.height / nh);
  const offX = (rect.width - nw * scale) / 2;
  const offY = (rect.height - nh * scale) / 2;
  const x = (clientX - rect.left - offX) / scale;
  const y = (clientY - rect.top - offY) / scale;
  if (x < 0 || y < 0 || x > nw || y > nh) return null;
  return { x, y, px: clientX - rect.left, py: clientY - rect.top };
}

function onPreviewTap(e) {
  if (appContainer.dataset.stage !== 'review' || !capturedBlob) return;
  const p = clientToImage(preview, e.clientX, e.clientY);
  if (!p) return;
  selectPoints.push([Math.round(p.x), Math.round(p.y), selectLabel]);
  const m = document.createElement('div');
  m.className = 'tap-marker' + (selectLabel === 0 ? ' is-negative' : '');
  m.style.left = `${p.px}px`;
  m.style.top = `${p.py}px`;
  tapMarkers.appendChild(m);
  $('selectHint').textContent = 'SELECTING…';
  clearTimeout(selectTimer);
  selectTimer = setTimeout(runSegment, 450);
}

async function runSegment() {
  if (!selectPoints.length || !capturedBlob) return;
  const seq = ++selectSeq;
  confirmBtn.disabled = true;
  try {
    const fd = new FormData();
    fd.append('image', capturedBlob, 'photo.png');
    fd.append('points', JSON.stringify(selectPoints));
    const res = await fetch(`${API_BASE}/segment`, { method: 'POST', body: fd });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    const blob = await res.blob();
    if (seq !== selectSeq) return;
    pendingIsolatedBlob = new Blob([blob], { type: 'image/png' });
    preview.src = URL.createObjectURL(pendingIsolatedBlob);
    $('selectHint').textContent = 'TAP MORE TO ADD · USE − REMOVE FOR EXTRA BITS';
  } catch (err) {
    if (seq !== selectSeq) return;
    $('selectHint').textContent = 'SELECTION FAILED. TAP AGAIN';
  } finally {
    if (seq === selectSeq) confirmBtn.disabled = false;
  }
}

async function autoSelect() {
  if (!capturedBlob) return;
  clearSelection();
  $('selectHint').textContent = 'AUTO-SELECTING…';
  confirmBtn.disabled = true;
  try {
    pendingIsolatedBlob = new Blob([await isolateObject(capturedBlob)], { type: 'image/png' });
    preview.src = URL.createObjectURL(pendingIsolatedBlob);
    $('selectHint').textContent = 'TAP THE OBJECT TO SELECT IT';
  } finally {
    confirmBtn.disabled = false;
  }
}

// ── Multi-view capture ───────────────────────────────────────────────────

function nextMissingView() { return MV_ORDER.find(v => !mvViews[v]); }

function renderMvStrip(busyView = null) {
  const next = nextMissingView();
  document.querySelectorAll('.mv-slot').forEach(slot => {
    const v = slot.dataset.view;
    slot.classList.toggle('is-filled', !!mvViews[v]);
    slot.classList.toggle('is-next', v === next && !busyView);
    slot.classList.toggle('is-busy', v === busyView);
    slot.style.backgroundImage = mvViews[v] ? `url(${mvViews[v].url})` : '';
  });
  $('mvHint').textContent = busyView ? 'Cutting out…' : (next ? MV_HINTS[next] : 'All 4 views ready. Tap GENERATE 3D');
  const count = MV_ORDER.filter(v => mvViews[v]).length;
  $('mvGenerate').disabled = !mvViews.front;
  const how = count > 1 ? 'MULTI-VIEW SHAPE' : '1 VIEW';
  $('mvGenerate').textContent = `Generate · ${count} view${count === 1 ? '' : 's'} · ${how.toLowerCase()}`;
}

async function captureView(targetView = null) {
  const view = targetView || nextMissingView();
  if (!view) return;
  captureBtn.disabled = true;
  try {
    const raw = await snapshotBlob();
    renderMvStrip(view);
    const cut = new Blob([await isolateObject(raw)], { type: 'image/png' });
    if (mvViews[view]) URL.revokeObjectURL(mvViews[view].url);
    mvViews[view] = { blob: cut, url: URL.createObjectURL(cut) };
  } catch (err) {
    showTaskToast('Could not cut out that view. Try again.', { error: true });
  } finally {
    captureBtn.disabled = false;
    renderMvStrip();
  }
}

function resetMultiview() {
  MV_ORDER.forEach(v => { if (mvViews[v]) URL.revokeObjectURL(mvViews[v].url); mvViews[v] = null; });
  renderMvStrip();
}

function generateMultiview() {
  const fd = new FormData();
  MV_ORDER.forEach(v => { if (mvViews[v]) fd.append(v, mvViews[v].blob, `${v}.png`); });
  appendSettings(fd);
  stopCamera();
  startGeneration(() => postForm('/generate3d', fd), mvViews.front.blob);
}

// ── Upload instead of camera ─────────────────────────────────────────────

async function onUploadFiles(files) {
  const imgs = [...files].filter(f => f.type.startsWith('image/'));
  if (!imgs.length) return;
  stopCamera();
  if (appMode === 'multiview') {
    for (const [i, f] of imgs.slice(0, 4).entries()) {
      const view = MV_ORDER[i];
      renderMvStrip(view);
      try {
        const cut = new Blob([await isolateObject(f)], { type: 'image/png' });
        mvViews[view] = { blob: cut, url: URL.createObjectURL(cut) };
      } catch (_) { /* skip */ }
    }
    renderMvStrip();
    return;
  }
  if (appMode === 'gallery') setMode('photo');
  await reviewPhoto(imgs[0]);
}

// ── Generation ───────────────────────────────────────────────────────────

async function postForm(path, fd) {
  const res = await fetch(`${API_BASE}${path}`, { method: 'POST', body: fd });
  if (!res.ok) throw new Error((await res.json().catch(() => ({ detail: res.statusText }))).detail);
  return res.json();
}

async function postJSON(path, body, method = 'POST') {
  const res = await fetch(`${API_BASE}${path}`, {
    method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}),
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({ detail: res.statusText }))).detail);
  return res.json();
}

async function fetchScan(id) {
  const res = await fetch(`${API_BASE}/scans/${id}`);
  if (!res.ok) throw new Error('Scan not found');
  return res.json();
}

async function startGeneration(createJob, seedBlob) {
  generationCancelled = false;
  preview.style.display = 'none';
  preview.src = '';
  hideModelReadyControls();
  showGenerationControls();
  captureBtn.disabled = true;
  modelViewerContainer.style.display = 'block';
  if (window.pointCloudLoader) {
    if (seedBlob) window.pointCloudLoader.setImageBlob(seedBlob);
    window.pointCloudLoader.start();
  }
  try {
    const { scan_id, job_id } = await createJob();
    currentScan = { id: scan_id };
    const done = await followJob(job_id, scan_id, {
      onProgress: (d) => updateGenerationProgress(`${d.stage.replace(/…$/, '')} · ${d.progress}%`, d.progress),
      onPreview: (url) => { if (window.pointCloudLoader) window.pointCloudLoader.assembleFromURL(absUrl(url)); },
    });
    if (generationCancelled) return;
    currentScan = await fetchScan(scan_id);
    relayModelUrl = done.local_url;
    modelViewer.setAttribute('src', absUrl(done.local_url));
    await Promise.all([
      new Promise(r => setTimeout(r, 1500)),
      new Promise(r => modelViewer.addEventListener('load', r, { once: true })),
    ]);
    if (generationCancelled) return;
    updateGenerationProgress('Done', 100);
    haptic([12, 60, 18]);
    if (window.pointCloudLoader) window.pointCloudLoader.crossfadeToModel();
    else { modelViewer.style.opacity = '1'; modelViewer.style.pointerEvents = ''; }
    hideGenerationControls();
    showModelReadyControls();
    saveThumbnailSoon();
  } catch (err) {
    if (generationCancelled) return;
    if (window.pointCloudLoader) window.pointCloudLoader.stop();
    hideGenerationControls();
    modelViewerContainer.style.display = 'none';
    captureBtn.disabled = false;
    setStage('idle');
    alert(`3D generation failed.\n\n${err.message}`);
    applyModeView();
  }
}

// Follow a local job's SSE stream until it finishes.
function followJob(jobId, scanId, { onProgress, onPreview } = {}) {
  return new Promise((resolve, reject) => {
    const es = new EventSource(`${API_BASE}/jobs/${jobId}/stream?scan_id=${encodeURIComponent(scanId)}`);
    activeStreams.push(es);
    es.addEventListener('progress', e => onProgress && onProgress(JSON.parse(e.data)));
    es.addEventListener('preview', e => onPreview && onPreview(JSON.parse(e.data).url));
    es.addEventListener('complete', e => { es.close(); resolve(JSON.parse(e.data)); });
    es.addEventListener('error', e => {
      es.close();
      let msg = 'Lost connection to the server';
      try { msg = JSON.parse(e.data).error || msg; } catch (_) { /* connection error */ }
      reject(new Error(msg));
    });
  });
}

function absUrl(u) { return u && u.startsWith('/') ? `${API_BASE}${u}` : u; }

function renderModelInfo() {
  const s = currentScan;
  if (!s || !s.model_url) { $('modelInfo').textContent = ''; return; }
  const bits = [
    s.name,
    'Hunyuan3D',
    (s.quality || '').toUpperCase(),
    s.faces ? `${Math.round(s.faces / 1000)}K faces` : null,
    s.seconds ? `${s.seconds}s` : null,
  ].filter(Boolean);
  $('modelInfo').textContent = bits.join(' · ');
}

// Save a viewer screenshot as the gallery thumbnail.
function saveThumbnailSoon() {
  if (!currentScan) return;
  const id = currentScan.id;
  setTimeout(async () => {
    try {
      const blob = await modelViewer.toBlob({ mimeType: 'image/png', idealAspect: true });
      const fd = new FormData();
      fd.append('image', blob, 'thumb.png');
      await fetch(`${API_BASE}/scans/${id}/thumbnail`, { method: 'POST', body: fd });
    } catch (_) { /* thumbnails are a nicety */ }
  }, 1200);
}

// ── Model tools ──────────────────────────────────────────────────────────

async function runTool(label, startFn, { newScan = false } = {}) {
  if (!currentScan || busyTool) return;
  busyTool = true;
  document.querySelectorAll('#toolsBar .chip').forEach(c => c.classList.add('is-busy'));
  try {
    const { scan_id, job_id } = await startFn();
    const done = await followJob(job_id, scan_id, {
      onProgress: d => showTaskToast(`${label}: ${d.stage} ${d.progress}%`, { sticky: true }),
    });
    currentScan = await fetchScan(scan_id);
    relayModelUrl = done.local_url;
    modelViewer.setAttribute('src', absUrl(done.local_url));
    renderModelInfo();
    showTaskToast(newScan ? 'New variation saved to your gallery.' : `${label}: done.`);
    saveThumbnailSoon();
  } catch (err) {
    showTaskToast(`${label} failed: ${err.message}`, { error: true });
  } finally {
    busyTool = false;
    document.querySelectorAll('#toolsBar .chip').forEach(c => c.classList.remove('is-busy'));
  }
}

function refineDetail(value) {
  $('toolDetail').value = '';
  if (!value) return;
  const body = value === 'notex' ? { texture: false } : { faces: parseInt(value, 10), texture: true };
  runTool('Detail', () => postJSON(`/scans/${currentScan.id}/refine`, body));
}

function makeVariation() {
  runTool('Variation', () => postJSON(`/scans/${currentScan.id}/variation`), { newScan: true });
}

function downloadUrl(url) {
  const a = document.createElement('a');
  a.href = url;
  a.download = '';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

async function arAsset(animation, heightCm) {
  return postJSON(`/scans/${currentScan.id}/ar`, { animation, height_cm: heightCm });
}

async function exportModel(fmt) {
  if (!fmt || !currentScan) return;
  if (fmt === 'usdz' || fmt === 'anim') {
    const anim = fmt === 'anim' ? ($('blenderAnim').value || 'idle') : ($('blenderAnim').value || 'none');
    showTaskToast('Building file…', { sticky: true });
    try {
      const r = await arAsset(anim === 'none' && fmt === 'anim' ? 'idle' : anim, blenderHeight());
      downloadUrl(API_BASE + (fmt === 'usdz' ? r.usdz_url : r.glb_url));
      showTaskToast('Download started.');
    } catch (err) { showTaskToast(`Export failed: ${err.message}`, { error: true }); }
    return;
  }
  const a = document.createElement('a');
  a.href = `${API_BASE}/scans/${currentScan.id}/export/${fmt}`;
  a.download = '';
  document.body.appendChild(a);
  a.click();
  a.remove();
  showTaskToast(`Preparing ${fmt.toUpperCase()} download…`);
}

async function renameModel() {
  if (!currentScan) return;
  const name = prompt('Name this scan', currentScan.name || '');
  if (!name || !name.trim()) return;
  try {
    currentScan = await postJSON(`/scans/${currentScan.id}`, { name: name.trim() }, 'PATCH');
    renderModelInfo();
  } catch (err) {
    showTaskToast(`Rename failed: ${err.message}`, { error: true });
  }
}

// Interactive AR playground (WebXR on Android, Quick Look on iPhone, 3D playground on laptops).
// ── Editor ───────────────────────────────────────────────────────────────
function sliderEdit() {
  return {
    brightness: +$('edBright').value, contrast: +$('edContrast').value, saturation: +$('edSat').value,
    hue: +$('edHue').value, roughness: +$('edRough').value, metallic: +$('edMetal').value, smooth: +$('edSmooth').value,
  };
}
function resetSliders() {
  [['edBright', 1], ['edContrast', 1], ['edSat', 1], ['edHue', 0], ['edRough', 0.75], ['edMetal', 0], ['edSmooth', 0]]
    .forEach(([id, v]) => { $(id).value = v; });
}
async function applyEdit(edit) {
  if (!currentScan || busyTool) return;
  busyTool = true;
  showTaskToast('Applying…', { sticky: true });
  try {
    currentScan = await postJSON(`/scans/${currentScan.id}/edit`, edit);
    relayModelUrl = currentScan.model_url;
    modelViewer.setAttribute('src', absUrl(currentScan.model_url) + `?v=${Date.now()}`);
    renderModelInfo();
    if (!edit.rotate && !edit.mirror) resetSliders();
    showTaskToast('Saved. Undo any time.');
    saveThumbnailSoon();
  } catch (err) {
    showTaskToast(`Edit failed: ${err.message}`, { error: true });
  } finally { busyTool = false; }
}
async function undoEdit() {
  if (!currentScan) return;
  try {
    currentScan = await postJSON(`/scans/${currentScan.id}/undo`);
    relayModelUrl = currentScan.model_url;
    modelViewer.setAttribute('src', absUrl(currentScan.model_url) + `?v=${Date.now()}`);
    renderModelInfo();
    showTaskToast('Undone.');
  } catch (err) { showTaskToast(err.message, { error: true }); }
}

// ── Blender with animation ───────────────────────────────────────────────
function blenderHeight() { return parseInt($('blenderSize').value, 10) || 100; }
async function loadAnimationChoices() {
  try {
    const { animations } = await (await fetch(`${API_BASE}/ar/presets`)).json();
    $('blenderAnim').innerHTML = animations.map(a => `<option value="${a}">${a === 'none' ? 'Still (no animation)' : a[0].toUpperCase() + a.slice(1)}</option>`).join('');
  } catch (_) { /* keep default */ }
}
async function sendToBlenderAnimated() {
  if (!currentScan) return;
  const anim = $('blenderAnim').value;
  const size = parseInt($('blenderSize').value, 10);
  let url = currentScan.model_url;
  try {
    if (anim !== 'none' || size) {
      showTaskToast('Preparing animated model…', { sticky: true });
      url = (await arAsset(anim, size || 100)).glb_url;
    }
    await postJSON('/relay', { target: 'blender', type: 'model', url, name: (currentScan.name || 'scan') + (anim !== 'none' ? `_${anim}` : ''), scan_id: currentScan.id });
    showTaskToast(anim !== 'none' ? `Sent with "${anim}" animation. Press Space in Blender to play.` : 'Sent. Check your Blender viewport.');
  } catch (err) {
    showTaskToast(/connected/i.test(err.message) ? 'Blender isn\'t connected yet. Follow the 3 steps above.' : `Send failed: ${err.message}`, { error: true });
  }
}

function openAR() {
  if (!currentScan) return;
  location.href = `/play.html?scan=${encodeURIComponent(currentScan.id)}`;
}

// Deep link: /?scan=<id> opens that scan (used by the AR page's back button).
async function openScanFromUrl() {
  const id = new URLSearchParams(location.search).get('scan');
  if (!id) return;
  try {
    const scan = await fetchScan(id);
    if (scan.model_url) openScan(scan);
  } catch (_) { /* ignore */ }
  history.replaceState(null, '', '/');
}

// ── Gallery ──────────────────────────────────────────────────────────────

async function loadGallery() {
  const grid = $('galleryGrid');
  try {
    const { scans } = await (await fetch(`${API_BASE}/scans`)).json();
    grid.innerHTML = '';
    scans.forEach(scan => grid.appendChild(galleryItem(scan)));
  } catch (err) {
    grid.innerHTML = '';
    $('galleryEmpty').textContent = 'Could not load scans.';
  }
}

function galleryItem(scan) {
  const el = document.createElement('div');
  const ready = !!scan.model_url;
  el.className = 'gallery-item' + (ready ? '' : ' is-pending');
  el.setAttribute('role', 'button');
  el.tabIndex = 0;
  if (scan.thumb_url) el.style.backgroundImage = `url(${scan.thumb_url})`;
  el.innerHTML = `
    <div class="gi-badges"><span class="gi-badge"></span><span class="gi-badge"></span></div>
    <button class="gi-delete" type="button" aria-label="Delete scan">✕</button>
    <div class="gi-meta"><span class="gi-name"></span><span class="gi-sub"></span></div>`;
  const [b1, b2] = el.querySelectorAll('.gi-badge');
  b1.textContent = scan.kind === 'multiview' ? `${(scan.views || []).length}-VIEW` : 'PHOTO';
  b2.textContent = (scan.quality || 'standard').toUpperCase();
  el.querySelector('.gi-name').textContent = scan.name;
  const date = new Date(scan.created * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  el.querySelector('.gi-sub').textContent = ready
    ? `${date}${scan.faces ? ` · ${Math.round(scan.faces / 1000)}K` : ''}`
    : (scan.status === 'failed' ? 'Failed' : 'Processing…');
  el.querySelector('.gi-delete').addEventListener('click', async (e) => {
    e.stopPropagation();
    if (!confirm(`Delete "${scan.name}" from this Mac?`)) return;
    await fetch(`${API_BASE}/scans/${scan.id}`, { method: 'DELETE' });
    el.remove();
  });
  const open = () => ready && openScan(scan);
  el.addEventListener('click', open);
  el.addEventListener('keydown', e => { if (e.key === 'Enter') open(); });
  return el;
}

function openScan(scan) {
  stopCamera();
  currentScan = scan;
  relayModelUrl = scan.model_url;
  if (window.pointCloudLoader) window.pointCloudLoader.stop();
  modelViewerContainer.style.display = 'block';
  modelViewer.setAttribute('src', absUrl(scan.model_url));
  modelViewer.style.opacity = '1';
  modelViewer.style.pointerEvents = '';
  preview.style.display = 'none';
  placeholder.style.display = 'none';
  showModelReadyControls();
  if (!scan.thumb || scan.thumb.startsWith('view_')) {
    modelViewer.addEventListener('load', saveThumbnailSoon, { once: true });
  }
}

function togglePhoneLink() {
  const box = $('phoneLink');
  const show = box.style.display === 'none';
  box.style.display = show ? 'flex' : 'none';
  if (show) {
    const url = phoneUrl || API_BASE;
    $('phoneUrl').textContent = url;
    const qr = $('phoneQr');
    qr.innerHTML = '';
    if (window.QRCode) new QRCode(qr, { text: url, width: 216, height: 216, correctLevel: QRCode.CorrectLevel.M });
  }
}

// ── Modes ────────────────────────────────────────────────────────────────

function setMode(mode) {
  if (appContainer.dataset.stage === 'ready') resetToStart();
  if (!['idle', 'ready'].includes(appContainer.dataset.stage)) return;
  appMode = mode;
  appContainer.dataset.mode = mode;
  document.querySelectorAll('.mode-tab').forEach(t => t.classList.toggle('is-active', t.dataset.mode === mode));
  applyModeView();
}

function applyModeView() {
  if (appMode === 'gallery') {
    stopCamera();
    placeholder.style.display = 'none';
    loadGallery();
  } else {
    if (appMode === 'multiview') renderMvStrip();
    if (!cameraActive) {
      placeholder.style.display = '';
      checkCameraPermission();
    }
  }
}

// ── Blender relay ────────────────────────────────────────────────────────

async function sendToBlender() {
  if (!relayModelUrl) return;
  sendBtn.disabled = true;
  try {
    showIsolateLoading();
    updateProgress('Sending to Blender...', 95);
    await postJSON('/relay', {
      target: 'blender', type: 'model', url: relayModelUrl,
      name: (currentScan && currentScan.name) || 'scan', scan_id: currentScan ? currentScan.id : null,
    });
    hideIsolateLoading();
    showTaskToast('Sent. Check your Blender viewport.');
  } catch (err) {
    hideIsolateLoading();
    alert('Failed to send to Blender. Is the Blender add-on connected?');
  } finally {
    sendBtn.disabled = false;
  }
}

async function isolateObject(blob) {
  const formData = new FormData();
  formData.append('image', blob, 'photo.png');
  const res = await fetch(`${API_BASE}/isolate`, { method: 'POST', body: formData });
  if (!res.ok) throw new Error((await res.json().catch(() => ({ detail: res.statusText }))).detail || 'Isolation failed');
  return res.blob();
}

async function loadConnectionInfo() {
  try {
    const res = await fetch(`${API_BASE}/connection-info`);
    if (!res.ok) return;
    const data = await res.json();
    if (blenderConnectUrl) blenderConnectUrl.textContent = data.ws_url;
    if (blenderCopyBtn) blenderCopyBtn.dataset.url = data.ws_url;
    phoneUrl = data.phone_url;
    updateConnectionStatus(!!(data.clients && data.clients.blender > 0));
  } catch (err) {
    updateConnectionStatus(false);
  }
}

function updateConnectionStatus(isConnected) {
  connectionStatus.classList.toggle('connected', isConnected);
  tooltip.textContent = isConnected ? 'Blender: Connected' : 'Blender: Disconnected';
  const hint = $('blenderConnectHint');
  if (hint) hint.style.display = isConnected ? 'none' : '';
}

async function loadEngines() {
  try {
    engineInfo = await (await fetch(`${API_BASE}/engines`)).json();
  } catch (_) { /* keep defaults */ }
  renderSettings();
}

async function checkCameraPermission() {
  try {
    const result = await navigator.permissions.query({ name: 'camera' });
    if (result.state === 'granted') startCamera();
    result.onchange = () => { if (result.state === 'granted' && !cameraActive) startCamera(); };
  } catch { /* Permissions API unavailable */ }
}

// ── Init ─────────────────────────────────────────────────────────────────

function init() {
  initSegmentTrack(segmentsTrack, segments);
  initSegmentTrack(generationSegmentsTrack, generationSegments);
  loadSettings();

  captureBtn.addEventListener('click', capture);
  sendBtn.addEventListener('click', sendToBlender);
  confirmBtn.addEventListener('click', confirmPhoto);
  retakeBtn.addEventListener('click', retakePhoto);
  isolateCancelBtn.addEventListener('click', cancelIsolate);
  cancelGenerationBtn.addEventListener('click', resetToStart);
  startAgainBtn.addEventListener('click', resetToStart);

  // Settings (collapsible on phones)
  $('settingsSummary').addEventListener('click', (e) => {
    e.stopPropagation();
    const open = !$('settingsBar').classList.contains('open');
    $('settingsBar').classList.toggle('open', open);
    $('settingsSummary').setAttribute('aria-expanded', String(open));
    haptic(8);
  });
  document.addEventListener('click', (e) => { if (!e.target.closest('#settingsBar')) closeSettings(); });
  document.querySelectorAll('[data-quality]').forEach(b => b.addEventListener('click', () => {
    settings.quality = b.dataset.quality; saveSettings(); renderSettings();
  }));
  $('textureToggle').addEventListener('click', () => { settings.texture = !settings.texture; saveSettings(); renderSettings(); });

  // Modes
  document.querySelectorAll('.mode-tab').forEach(t => t.addEventListener('click', () => setMode(t.dataset.mode)));

  // Tap-to-select
  preview.addEventListener('click', onPreviewTap);
  $('selKeep').addEventListener('click', () => setSelectLabel(1));
  $('selRemove').addEventListener('click', () => setSelectLabel(0));
  $('selAuto').addEventListener('click', autoSelect);

  // Multi-view
  $('mvReset').addEventListener('click', resetMultiview);
  $('mvGenerate').addEventListener('click', generateMultiview);
  document.querySelectorAll('.mv-slot').forEach(slot => slot.addEventListener('click', () => {
    if (cameraActive && mvViews.front) captureView(slot.dataset.view);
  }));

  // Upload photos (button + drag & drop)
  const fileInput = $('fileInput');
  $('uploadBtn').addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', () => { onUploadFiles(fileInput.files); fileInput.value = ''; });
  document.addEventListener('dragover', e => { e.preventDefault(); });
  document.addEventListener('drop', e => {
    e.preventDefault();
    if (['idle'].includes(appContainer.dataset.stage)) onUploadFiles(e.dataTransfer.files);
  });

  // Gallery
  $('phoneLinkBtn').addEventListener('click', togglePhoneLink);

  // Tools
  $('toolAR').addEventListener('click', openAR);
  $('toolDetail').addEventListener('change', e => refineDetail(e.target.value));
  $('toolVariation').addEventListener('click', makeVariation);
  document.querySelectorAll('[data-export]').forEach(b => b.addEventListener('click', () => exportModel(b.dataset.export)));

  // Sheet tabs
  document.querySelectorAll('#sheetTabs button').forEach(b => b.addEventListener('click', () => {
    document.querySelectorAll('#sheetTabs button').forEach(x => x.classList.toggle('is-active', x === b));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('is-active', p.dataset.panel === b.dataset.tab));
  }));

  // Editor
  document.querySelectorAll('[data-rot]').forEach(b => b.addEventListener('click', () => applyEdit({ rotate: b.dataset.rot.split(',').map(Number) })));
  $('editMirror').addEventListener('click', () => applyEdit({ mirror: true }));
  $('editApply').addEventListener('click', () => applyEdit(sliderEdit()));
  $('editReset').addEventListener('click', resetSliders);
  $('editUndo').addEventListener('click', undoEdit);

  // Blender
  $('blenderSendAnim').addEventListener('click', sendToBlenderAnimated);
  loadAnimationChoices();
  $('toolRename').addEventListener('click', renameModel);

  if (blenderCopyBtn) {
    const copyToast = $('copyToast');
    let toastTimeout = null;
    blenderCopyBtn.addEventListener('click', () => {
      const url = blenderCopyBtn.dataset.url;
      if (!url) return;
      navigator.clipboard.writeText(url).then(() => {
        copyToast.classList.add('show');
        clearTimeout(toastTimeout);
        toastTimeout = setTimeout(() => copyToast.classList.remove('show'), 1800);
      });
    });
  }

  placeholder.addEventListener('click', () => { if (!cameraActive) startCamera(); });

  const flipBtn = $('flipBtn');
  if (flipBtn) {
    flipBtn.addEventListener('click', async () => {
      if (!cameraActive) return;
      facingMode = facingMode === 'environment' ? 'user' : 'environment';
      stopCamera();
      await startCamera();
    });
  }

  setStage('idle');
  renderSettings();
  loadEngines();
  loadConnectionInfo();
  setInterval(loadConnectionInfo, 3000);
  checkCameraPermission();
  openScanFromUrl();
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();
