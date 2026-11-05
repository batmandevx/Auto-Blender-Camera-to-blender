"""
Local AI for scans. Nothing leaves this Mac.

  identify()   Gemma 3 (vision, via Ollama) looks at the photo → name, category, personality, voice
  chat()       Gemma 3 role-plays as the object → short reply + an animation to play
  transcribe() Whisper large-v3-turbo on Apple MLX → text from your voice
  speak()      Kokoro (ONNX) → natural speech WAV

Ollama must be running (`ollama serve`) with `gemma3:4b` pulled.
"""
import base64
import io
import json
import os
import pathlib
import subprocess
import tempfile
import threading
import urllib.request
from typing import Dict, List, Optional

ROOT = pathlib.Path(__file__).parent
OLLAMA = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
LLM_MODEL = os.getenv("AI_MODEL", "gemma3:4b")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo")
KOKORO_DIR = ROOT / "models" / "kokoro"

CATEGORIES = ["character", "weapon", "vehicle", "animal", "food", "object"]
ACTIONS = ["none", "wave", "jump", "dance", "spin", "nod", "shake", "happy", "sad", "think", "backflip", "celebrate"]
# Kokoro voices that suit different kinds of objects
VOICES = {
    "cute": "af_sky", "warm": "af_heart", "bright": "af_bella", "calm": "af_nicole",
    "boy": "am_puck", "deep": "am_michael", "gruff": "am_fenrir", "british": "bm_george",
}

_tts = None
_tts_lock = threading.Lock()
_stt_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# Ollama helpers
# ─────────────────────────────────────────────────────────────────────────────
def _ollama(path: str, body: dict, timeout: float = 120) -> dict:
    req = urllib.request.Request(f"{OLLAMA}{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def status() -> dict:
    out = {"ollama": False, "model": LLM_MODEL, "model_ready": False,
           "tts": (KOKORO_DIR / "kokoro-v1.0.onnx").exists() and (KOKORO_DIR / "voices-v1.0.bin").exists(),
           "stt": True, "voices": VOICES}
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=2) as r:
            tags = json.loads(r.read())
        out["ollama"] = True
        out["model_ready"] = any(m.get("name", "").startswith(LLM_MODEL) for m in tags.get("models", []))
    except Exception:
        pass
    return out


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1]) if start >= 0 else {}


# ─────────────────────────────────────────────────────────────────────────────
# Identify
# ─────────────────────────────────────────────────────────────────────────────
IDENTIFY_PROMPT = """Look at this photo of a single object that was turned into a 3D model.
Reply with ONLY a JSON object:
{
  "name": short friendly name for it (e.g. "Pinky the octopus", "Toy blaster"),
  "what": what it is in 2-5 words,
  "category": one of %s,
  "is_toy_or_character": true if it is a doll, plush, figure, mascot, toy animal or anything with a face,
  "is_gun": true if it is any kind of gun, blaster, pistol, rifle or toy gun,
  "personality": one sentence describing how it would talk if it came alive (fun and friendly),
  "voice": one of %s,
  "colors": main colours, comma separated
}""" % (CATEGORIES, list(VOICES))


def identify(image_bytes: bytes) -> dict:
    res = _ollama("/api/generate", {
        "model": LLM_MODEL, "prompt": IDENTIFY_PROMPT, "images": [base64.b64encode(image_bytes).decode()],
        "format": "json", "stream": False, "options": {"temperature": 0.3}, "keep_alive": 0,
    }, timeout=180)
    info = _parse_json(res.get("response", "{}"))
    cat = info.get("category", "object")
    if info.get("is_gun"):
        cat = "weapon"
    elif info.get("is_toy_or_character") and cat not in ("animal",):
        cat = "character"
    info["category"] = cat if cat in CATEGORIES else "object"
    info["role"] = "gun" if info.get("is_gun") else ("character" if cat in ("character", "animal") else "object")
    info["voice"] = info.get("voice") if info.get("voice") in VOICES else ("cute" if info["role"] == "character" else "deep")
    info.setdefault("name", "Scan")
    info.setdefault("personality", "Cheerful, curious and a little silly.")
    return info


# ─────────────────────────────────────────────────────────────────────────────
# Chat
# ─────────────────────────────────────────────────────────────────────────────
def chat(ai: dict, message: str, history: List[Dict[str, str]]) -> dict:
    persona = (
        f"You are {ai.get('name', 'a toy')}, {ai.get('what', 'an object')} that has come alive as a 3D model "
        f"in augmented reality. Personality: {ai.get('personality', 'friendly')}. "
        "You are talking to the person who scanned you. Stay in character, be playful and warm, "
        "keep every reply to 1-2 short spoken sentences (no emojis, no markdown, no stage directions). "
        "Reply with ONLY JSON: {\"reply\": what you say, \"action\": one of " + json.dumps(ACTIONS) +
        " that matches your mood}."
    )
    msgs = [{"role": "system", "content": persona}]
    for h in history[-10:]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            msgs.append({"role": h["role"], "content": str(h["content"])[:600]})
    msgs.append({"role": "user", "content": message[:800]})
    res = _ollama("/api/chat", {"model": LLM_MODEL, "messages": msgs, "format": "json", "stream": False,
                                "options": {"temperature": 0.8, "num_predict": 160}}, timeout=120)
    out = _parse_json(res.get("message", {}).get("content", "{}"))
    reply = str(out.get("reply") or "Hmm?").strip()
    action = out.get("action") if out.get("action") in ACTIONS else "none"
    return {"reply": reply, "action": action}


# ─────────────────────────────────────────────────────────────────────────────
# Speech
# ─────────────────────────────────────────────────────────────────────────────
def transcribe(audio_bytes: bytes) -> str:
    """Any browser audio (webm/ogg/mp4/wav) → text."""
    import mlx_whisper
    with tempfile.TemporaryDirectory() as tmp:
        src = pathlib.Path(tmp) / "in.bin"
        wav = pathlib.Path(tmp) / "in.wav"
        src.write_bytes(audio_bytes)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-ar", "16000", "-ac", "1", str(wav)],
                       check=True, timeout=60)
        with _stt_lock:
            result = mlx_whisper.transcribe(str(wav), path_or_hf_repo=WHISPER_MODEL, language="en",
                                            condition_on_previous_text=False)
    return (result.get("text") or "").strip()


def speak(text: str, voice: str = "cute", speed: float = 1.0) -> bytes:
    global _tts
    import soundfile as sf
    from kokoro_onnx import Kokoro
    with _tts_lock:
        if _tts is None:
            _tts = Kokoro(str(KOKORO_DIR / "kokoro-v1.0.onnx"), str(KOKORO_DIR / "voices-v1.0.bin"))
        samples, rate = _tts.create(text[:600], voice=VOICES.get(voice, voice if "_" in voice else "af_sky"),
                                    speed=float(min(max(speed, 0.6), 1.6)), lang="en-us")
    buf = io.BytesIO()
    sf.write(buf, samples, rate, format="WAV")
    return buf.getvalue()
