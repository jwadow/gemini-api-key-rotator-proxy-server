# main-openai.py
# FastAPI local OpenAI-compatible -> Gemini proxy with rotating keys + optional thinking chain
# pip install fastapi uvicorn httpx

import os
import time
import asyncio
import json
import random
from typing import List, Optional, Dict, Any, Tuple

from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import Response, JSONResponse, StreamingResponse
import httpx

APP = FastAPI(title="Local OpenAI-compatible -> Gemini proxy (with optional thinking chain)")

# -------------------------
# Config
# -------------------------
VPN_PROXY_URL = ""  # proxy to bypass regional restrictions, for example "192.168.1.103:2080" or "" to disable
KEYS_FILE = "api_keys.txt" # api keys, one per line
ADMIN_TOKEN = "changeme_local_only"
UPSTREAM_BASE_GEMINI = "https://generativelanguage.googleapis.com/v1beta"
BACKOFF_MIN = 5
BACKOFF_MAX = 600
DEBUG = False

# Force enabling of thinking chain parameters
ENABLE_THINKING_CHAIN = False
'''
"ENABLE_THINKING_CHAIN = True" - Not works with Roo Code (Unexpected API Response).
But in the OpenAI Python library (test.py) it works perfectly without manually passing:

extra_body={
    'extra_body': {
    "google": {
        "thinking_config": {
        "thinking_budget": 32768, # 128 to 32768
        "include_thoughts": True
        }
    }
    }
}
'''

# -------------------------
# Setup proxy from config
# -------------------------
if VPN_PROXY_URL:
    proxy_url_with_scheme = VPN_PROXY_URL if "://" in VPN_PROXY_URL else f"http://{VPN_PROXY_URL}"
    os.environ['HTTP_PROXY'] = proxy_url_with_scheme
    os.environ['HTTPS_PROXY'] = proxy_url_with_scheme
    os.environ['ALL_PROXY'] = proxy_url_with_scheme

# -------------------------
# Utilities: load keys
# -------------------------
def load_keys_from_file(path: str) -> List[str]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"API keys file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        keys = [line.strip() for line in f if line.strip()]
    if not keys:
        raise RuntimeError("No API keys found in file.")
    return keys

KEYS_LIST = load_keys_from_file(KEYS_FILE)

# -------------------------
# Key state & pool
# -------------------------
class KeyState:
    def __init__(self, key: str):
        self.key: str = key
        self.backoff: float = 0.0
        self.banned_until: float = 0.0
        self.success: int = 0
        self.fail: int = 0

    def is_available(self) -> bool:
        return time.monotonic() >= self.banned_until

    def mark_success(self) -> None:
        self.backoff = 0.0
        self.banned_until = 0.0
        self.success += 1

    def mark_failure(self) -> None:
        if self.backoff <= 0:
            self.backoff = BACKOFF_MIN
        else:
            self.backoff = min(BACKOFF_MAX, self.backoff * 2.0)
        self.banned_until = time.monotonic() + self.backoff
        self.fail += 1


class KeyPool:
    def __init__(self, keys: List[str]):
        self.states: List[KeyState] = [KeyState(k) for k in keys]
        self.n: int = len(self.states)
        self.idx: int = 0
        self.lock = asyncio.Lock()

    async def next_available(self) -> Optional[KeyState]:
        async with self.lock:
            start = self.idx
            for i in range(self.n):
                j = (start + i) % self.n
                st = self.states[j]
                if st.is_available():
                    self.idx = (j + 1) % self.n
                    return st
            return None

    def status(self) -> List[Dict[str, Any]]:
        now = time.monotonic()
        out: List[Dict[str, Any]] = []
        for s in self.states:
            out.append({
                "key_preview": s.key[:8] + "..." if len(s.key) > 8 else s.key,
                "available_in": max(0, round(s.banned_until - now, 2)),
                "backoff": s.backoff,
                "success": s.success,
                "fail": s.fail,
            })
        return out


POOL = KeyPool(KEYS_LIST)

# -------------------------
# Upstream streaming
# -------------------------
async def stream_from_upstream(method: str, url: str, headers: Dict[str, str], content: Optional[bytes], key_state: KeyState, timeout: int = 300):
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            async with client.stream(method, url, headers=headers, content=content) as upstream:
                if upstream.status_code >= 400:
                    body = await upstream.aread()
                    key_state.mark_failure()
                    if body:
                        yield body
                    return
                key_state.mark_success()
                async for chunk in upstream.aiter_bytes():
                    if chunk:
                        yield chunk
        except httpx.RequestError:
            key_state.mark_failure()
            raise

async def try_forward_to_upstream(method: str, url: str, headers: Dict[str, str], content: Optional[bytes], is_stream: bool, key_state: KeyState, timeout: int = 300):
    if is_stream:
        gen = stream_from_upstream(method, url, headers, content, key_state, timeout=timeout)
        return StreamingResponse(gen, media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})
    else:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, url, headers=headers, content=content)
            if resp.status_code in (429, 403, 500, 502, 503):
                key_state.mark_failure()
            else:
                key_state.mark_success()
            media_type = resp.headers.get("content-type", "application/json")
            return Response(content=resp.content, status_code=resp.status_code, media_type=media_type)

# -------------------------
# Map incoming path to upstream
# -------------------------
def map_incoming_to_upstream(path: str) -> str:
    p = path.lstrip("/")
    if p.startswith("v1/"):
        p = p[len("v1/") :]
    if p == "models" or p.startswith("models/"):
        return UPSTREAM_BASE_GEMINI.rstrip("/") + "/openai/models"
    return UPSTREAM_BASE_GEMINI.rstrip("/") + "/openai/" + p

def detect_stream_from_request(content_bytes: Optional[bytes], query_params: Dict[str, Any]) -> bool:
    qp = query_params.get("stream")
    if qp in ("true", "True", "1", True):
        return True
    if content_bytes:
        try:
            j = json.loads(content_bytes.decode(errors="ignore"))
            if isinstance(j, dict) and j.get("stream") is True:
                return True
        except Exception:
            pass
    return False

# -------------------------
# Catch-all proxy
# -------------------------
@APP.api_route("/{full_path:path}", methods=["GET","POST","PUT","DELETE","PATCH","OPTIONS"])
async def catch_all(request: Request, full_path: str):
    upstream_url = map_incoming_to_upstream(full_path)
    content = await request.body()
    params = dict(request.query_params)
    is_stream = detect_stream_from_request(content if content else None, params)

    incoming_headers: Dict[str, str] = {k:v for k,v in request.headers.items() if k.lower() not in ("host","content-length","transfer-encoding","connection")}

    # /models -> random available key
    p_normal = full_path[len("v1/"):] if full_path.startswith("v1/") else full_path
    if p_normal == "models" or p_normal.startswith("models/"):
        avail = [s for s in POOL.states if s.is_available()]
        key_state = random.choice(avail) if avail else min(POOL.states, key=lambda s: s.banned_until)
        headers = dict(incoming_headers)
        headers["Authorization"] = f"Bearer {key_state.key}"
        if not any(k.lower()=="content-type" for k in headers):
            ct = request.headers.get("content-type")
            headers["Content-Type"] = ct if ct else "application/json"
        return await try_forward_to_upstream(request.method, upstream_url, headers, content, is_stream, key_state)

    # Normal round-robin keys
    tried: List[str] = []
    for _ in range(len(POOL.states)):
        key_state = await POOL.next_available()
        if key_state is None:
            break
        tried.append(key_state.key[:8]+"...")
        headers = dict(incoming_headers)
        headers["Authorization"] = f"Bearer {key_state.key}"
        if not any(k.lower()=="content-type" for k in headers):
            ct = request.headers.get("content-type")
            headers["Content-Type"] = ct if ct else "application/json"

        # Правильная вставка thinking chain, если включено
        body_to_send = content
        if ENABLE_THINKING_CHAIN and content:
            try:
                body_json = json.loads(content.decode())
                # добавить thinking_config только если его нет
                if "extra_body" not in body_json or "google" not in body_json.get("extra_body", {}):
                    body_json.setdefault("extra_body", {}).setdefault("google", {}).setdefault("thinking_config", {
                        "thinking_budget": 32768,
                        "include_thoughts": True
                    })
                    body_to_send = json.dumps(body_json).encode("utf-8")
            except Exception:
                # если парсинг не удался, просто отправляем оригинальный content
                body_to_send = content

        try:
            return await try_forward_to_upstream(request.method, upstream_url, headers, body_to_send, is_stream, key_state)
        except Exception:
            key_state.mark_failure()
            continue

    return JSONResponse({"error":"all keys unavailable", "tried": tried}, status_code=429)


# -------------------------
# Admin endpoints
# -------------------------
def is_admin(auth_header: Optional[str]) -> bool:
    if not auth_header:
        return False
    if auth_header == ADMIN_TOKEN:
        return True
    if auth_header.lower().startswith("bearer "):
        return auth_header.split(" ",1)[1] == ADMIN_TOKEN
    return False

@APP.get("/status")
async def status(x_proxy_admin: Optional[str] = Header(None)):
    if not is_admin(x_proxy_admin):
        raise HTTPException(401, "Unauthorized")
    return JSONResponse({"keys": POOL.status()})

@APP.post("/reload-keys")
async def reload_keys(x_proxy_admin: Optional[str] = Header(None)):
    if not is_admin(x_proxy_admin):
        raise HTTPException(401, "Unauthorized")
    global KEYS_LIST, POOL
    KEYS_LIST = load_keys_from_file(KEYS_FILE)
    POOL = KeyPool(KEYS_LIST)
    return JSONResponse({"reloaded": True, "num_keys": len(KEYS_LIST)})

# -------------------------
# Run note:
# uvicorn main-openai:APP --host 127.0.0.1 --port 8000
# -------------------------
