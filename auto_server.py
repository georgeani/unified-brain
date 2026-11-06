"""Start a local query service for each checkout used by this MCP process."""
import atexit
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx

BASE = Path(__file__).resolve().parent
_services = {}


def _stop_services():
    for process, _ in _services.values():
        if process.poll() is None:
            process.terminate()


atexit.register(_stop_services)


def service_url(db_path):
    db_path = str(Path(db_path).resolve())
    existing = _services.get(db_path)
    if existing and existing[0].poll() is None:
        return existing[1]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    env = {**os.environ, "DB_PATH": db_path, "PORT": str(port)}
    process = subprocess.Popen([sys.executable, str(BASE / "codebase_qa_service.py")],
                               cwd=str(BASE), env=env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        try:
            response = httpx.get(f"{url}/health", timeout=1)
            if response.status_code == 200 and response.json().get("db") == db_path:
                _services[db_path] = (process, url)
                return url
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(0.1)
    process.terminate()
    raise RuntimeError("Could not start the repository's query service; check installed dependencies and settings")
