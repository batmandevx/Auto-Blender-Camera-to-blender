"""
Run the relay server on two ports from ONE process (so phone and Blender share connections):

  http://localhost:8000      laptop browser + Blender (ws://localhost:8000/ws?client=blender)
  https://<your-LAN-IP>:8443 phones on the same Wi-Fi (camera needs HTTPS) — no ngrok needed

The HTTPS certificate is self-signed and created on first run in ./certs. Your phone will
warn about it once: tap "Show details → visit this website" (iOS) or "Advanced → Proceed" (Android).

Usage:  python serve.py            (add --no-https to serve HTTP only)
"""
import asyncio
import contextlib
import os
import signal
import pathlib
import subprocess
import sys

import uvicorn
from dotenv import load_dotenv

load_dotenv()

ROOT = pathlib.Path(__file__).parent
CERT_DIR = ROOT / "certs"
HTTP_PORT = int(os.getenv("PORT", "8000"))
HTTPS_PORT = int(os.getenv("HTTPS_PORT", "8443"))


def ensure_cert(ip: str | None) -> tuple[str, str]:
    CERT_DIR.mkdir(exist_ok=True)
    key, crt = CERT_DIR / "key.pem", CERT_DIR / "cert.pem"
    stamp = CERT_DIR / "ip.txt"
    if key.exists() and crt.exists() and stamp.exists() and stamp.read_text() == (ip or ""):
        return str(key), str(crt)
    san = "DNS:localhost,IP:127.0.0.1" + (f",IP:{ip}" if ip else "")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "825",
         "-keyout", str(key), "-out", str(crt), "-subj", "/CN=camera-to-blender",
         "-addext", f"subjectAltName={san}"],
        check=True, capture_output=True,
    )
    stamp.write_text(ip or "")
    return str(key), str(crt)


class Server(uvicorn.Server):
    """Let main() handle Ctrl+C so both servers stop together."""

    def capture_signals(self):
        return contextlib.nullcontext()


def ensure_ollama() -> None:
    """Start the local AI (Ollama) if it's installed but not running."""
    import shutil
    import socket
    with socket.socket() as sock:
        sock.settimeout(0.5)
        if sock.connect_ex(("127.0.0.1", 11434)) == 0:
            return
    exe = shutil.which("ollama")
    if not exe:
        print("  (Ollama not found: AI chat is off. Install it from ollama.com, then `ollama pull gemma3:4b`.)")
        return
    subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    print("  Started local AI (Ollama).")


async def main() -> None:
    from relay_server import app, lan_ip

    ensure_ollama()

    ip = lan_ip()
    servers = [Server(uvicorn.Config(app, host="0.0.0.0", port=HTTP_PORT, log_level="info"))]
    if "--no-https" not in sys.argv:
        key, crt = ensure_cert(ip)
        servers.append(Server(uvicorn.Config(
            app, host="0.0.0.0", port=HTTPS_PORT, ssl_keyfile=key, ssl_certfile=crt, log_level="info")))

    print("\n  Camera → 3D → Blender")
    print(f"  Laptop:  http://localhost:{HTTP_PORT}")
    if len(servers) > 1 and ip:
        print(f"  Phone:   https://{ip}:{HTTPS_PORT}   (same Wi-Fi; accept the certificate warning once)")
    print(f"  Blender: ws://localhost:{HTTP_PORT}/ws?client=blender\n")
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: [setattr(s, "should_exit", True) for s in servers])
    await asyncio.gather(*(s.serve() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())
