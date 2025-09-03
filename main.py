# main.py
# FastAPI native Gemini proxy with rotating keys + API-key vs OAuth handling
# pip install fastapi uvicorn httpx

import os
import time
import asyncio
import json
import random
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import Response, JSONResponse, StreamingResponse
import httpx

APP = FastAPI(title="Native Gemini proxy (auth-mode auto-detect)")

# -------------------------
# Config
# -------------------------
VPN_PROXY_URL = "192.168.1.103:2080"  # proxy to bypass regional restrictions, for example "192.168.1.103:2080" or "" to disable
KEYS_FILE = "api_keys.txt" # api keys, one per line
ADMIN_TOKEN = "changeme_local_only"
UPSTREAM_BASE_GEMINI = "https://generativelanguage.googleapis.com/v1beta"
BACKOFF_MIN = 5
BACKOFF_MAX = 600
DEBUG = True

# -------------------------
# Setup proxy from config (optional http proxy)
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
# Key state & pool (simple backoff-based)
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
                "key_preview": (s.key[:8] + "...") if len(s.key) > 8 else s.key,
                "available_in": max(0, round(s.banned_until - now, 2)),
                "backoff": s.backoff,
                "success": s.success,
                "fail": s.fail,
            })
        return out


POOL = KeyPool(KEYS_LIST)

# -------------------------
# Upstream forwarding helpers
# -------------------------
async def stream_from_upstream(
    method: str,
    upstream_url: str,
    headers: Dict[str, str],
    params: Dict[str, Any],
    content: Optional[bytes],
    key_state: KeyState,
    timeout: int = 300,
):
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            async with client.stream(method, upstream_url, headers=headers, params=params, content=content) as upstream:
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


async def try_forward_to_upstream(
    method: str,
    upstream_url: str,
    headers: Dict[str, str],
    params: Dict[str, Any],
    content: Optional[bytes],
    is_stream: bool,
    key_state: KeyState,
    timeout: int = 300,
):
    """
    Forward request to upstream using provided headers and params (already prepared).
    """
    if is_stream:
        gen = stream_from_upstream(method, upstream_url, headers, params, content, key_state, timeout=timeout)
        return StreamingResponse(gen, media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})
    else:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, upstream_url, headers=headers, params=params, content=content)
            if resp.status_code in (429, 403, 500, 502, 503):
                key_state.mark_failure()
            else:
                key_state.mark_success()
            media_type = resp.headers.get("content-type", "application/json")
            return Response(content=resp.content, status_code=resp.status_code, media_type=media_type)


# -------------------------
# Routing helpers (fixed: avoid double v1/v1beta)
# -------------------------
def map_incoming_to_upstream(path: str) -> str:
    """
    Map incoming path -> native Gemini upstream URL.
    Strip leading 'v1/' or 'v1beta/' if present to avoid duplication.
    """
    p = path.lstrip("/")
    if p.startswith("v1/"):
        p = p[len("v1/"):]
    elif p.startswith("v1beta/"):
        p = p[len("v1beta/"):]
    # avoid trailing slash duplication
    if p == "":
        return UPSTREAM_BASE_GEMINI.rstrip("/")
    return UPSTREAM_BASE_GEMINI.rstrip("/") + "/" + p


def detect_stream_from_request(content_bytes: Optional[bytes], query_params: Dict[str, Any]) -> bool:
    # Gemini native streaming uses alt=sse
    if query_params.get("alt") == "sse":
        return True
    # Also support stream=true for compatibility with some clients
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


def prepare_auth_for_key(incoming_headers: Dict[str, str], incoming_params: Dict[str, Any], key_state: KeyState):
    """
    Return (headers_copy, params_copy) where authentication for key_state.key is applied.
    - If key looks like API key (starts with 'AIza'), put it as params['key'].
    - Otherwise set Authorization: Bearer <key>.
    """
    headers = dict(incoming_headers)
    params = dict(incoming_params) if incoming_params is not None else {}

    k = key_state.key.strip()
    # heuristic: Google API keys usually start with "AIza"
    if k.startswith("AIza"):
        # use query parameter 'key' for API key (do not set Authorization)
        params['key'] = k
        if 'authorization' in {x.lower() for x in headers.keys()}:
            # remove incoming Authorization to avoid confusion
            headers = {hk: hv for hk, hv in headers.items() if hk.lower() != 'authorization'}
        auth_mode = "api_key(query)"
    else:
        # assume OAuth access token / service account token etc.
        headers['Authorization'] = f"Bearer {k}"
        auth_mode = "bearer_header"
    if DEBUG:
        print(f"[DEBUG] auth mode {auth_mode} for key preview {k[:12]}...")
    return headers, params


# -------------------------
# Catch-all proxy endpoint
# -------------------------
@APP.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def catch_all(request: Request, full_path: str):
    upstream_url = map_incoming_to_upstream(full_path)

    content = await request.body()
    params = dict(request.query_params)
    is_stream = detect_stream_from_request(content if content else None, params)

    # If streaming is requested, modify the upstream URL and clean up params
    if is_stream and ":generateContent" in upstream_url:
        upstream_url = upstream_url.replace(":generateContent", ":streamGenerateContent")
        # clean up param that Gemini API does not use, to avoid potential errors
        if 'stream' in params:
            del params['stream']

    # copy incoming headers but skip hop-by-hop
    incoming_headers: Dict[str, str] = {}
    for k, v in request.headers.items():
        if k.lower() in ("host", "content-length", "transfer-encoding", "connection"):
            continue
        incoming_headers[k] = v

    tried: List[str] = []
    errors: List[Dict[str, Any]] = []

    # Normalize path for /models detection (strip leading v1/v1beta if present)
    p_normal = full_path.lstrip("/")
    if p_normal.startswith("v1/"):
        p_normal = p_normal[len("v1/"):]
    elif p_normal.startswith("v1beta/"):
        p_normal = p_normal[len("v1beta/"):]

    # Special handling for models listing: use random available key (prefer available)
    if p_normal == "models" or p_normal.startswith("models/"):
        avail = [s for s in POOL.states if s.is_available()]
        key_state = random.choice(avail) if avail else min(POOL.states, key=lambda s: s.banned_until)
        tried.append(key_state.key[:8] + "...")
        headers_auth, params_auth = prepare_auth_for_key(incoming_headers, params, key_state)

        # ensure Content-Type present
        if not any(k.lower() == "content-type" for k in headers_auth.keys()):
            ct = request.headers.get("content-type")
            headers_auth["Content-Type"] = ct if ct else "application/json"

        if DEBUG:
            print(f"[DEBUG] /models using key {key_state.key[:12]}..., forwarding to {upstream_url} with params={params_auth}")

        try:
            result = await try_forward_to_upstream(
                method=request.method,
                upstream_url=upstream_url,
                headers=headers_auth,
                params=params_auth,
                content=content,
                is_stream=is_stream,
                key_state=key_state,
            )
            return result
        except httpx.RequestError as e:
            key_state.mark_failure()
            errors.append({"key_preview": key_state.key[:8] + "...", "error": str(e)})
            return JSONResponse({"error": "models upstream request failed", "tried": tried, "errors": errors}, status_code=502)
        except Exception as e:
            key_state.mark_failure()
            errors.append({"key_preview": key_state.key[:8] + "...", "error": str(e)})
            return JSONResponse({"error": "models upstream request failed", "tried": tried, "errors": errors}, status_code=502)

    # Normal round-robin flow: try next available key
    for _ in range(len(POOL.states)):
        key_state = await POOL.next_available()
        if key_state is None:
            break
        tried.append(key_state.key[:8] + "...")
        headers_auth, params_auth = prepare_auth_for_key(incoming_headers, params, key_state)

        # ensure Content-Type present
        if not any(k.lower() == "content-type" for k in headers_auth.keys()):
            ct = request.headers.get("content-type")
            headers_auth["Content-Type"] = ct if ct else "application/json"

        if DEBUG:
            print(f"[DEBUG] trying key {key_state.key[:12]}... -> {upstream_url} params={params_auth}")

        try:
            result = await try_forward_to_upstream(
                method=request.method,
                upstream_url=upstream_url,
                headers=headers_auth,
                params=params_auth,
                content=content,
                is_stream=is_stream,
                key_state=key_state,
            )

            # For non-streaming responses, check for retryable errors. If found, continue to next key.
            # Note: Streaming responses with errors will still be passed to the client without retry.
            if isinstance(result, Response) and not isinstance(result, StreamingResponse):
                if result.status_code in (429, 403, 500, 502, 503):
                    # key_state.mark_failure() is already called inside try_forward_to_upstream
                    try:
                        error_body = json.loads(result.body)
                        errors.append({"key_preview": key_state.key[:8] + "...", "error": error_body, "status_code": result.status_code})
                    except:
                        errors.append({"key_preview": key_state.key[:8] + "...", "error": bytes(result.body).decode(errors='ignore'), "status_code": result.status_code})
                    if DEBUG:
                        print(f"[DEBUG] Key {key_state.key[:12]}... failed with status {result.status_code}. Retrying with next key.")
                    continue  # Try next key

            # This is a success, a non-retryable error, or a stream -> return immediately
            return result
        except httpx.RequestError as e:
            key_state.mark_failure()
            errors.append({"key_preview": key_state.key[:8] + "...", "error": str(e)})
            continue
        except Exception as e:
            key_state.mark_failure()
            errors.append({"key_preview": key_state.key[:8] + "...", "error": str(e)})
            continue

    # no key succeeded or none available
    if not tried:
        return JSONResponse({"error": "all keys rate-limited or in backoff"}, status_code=429)
    return JSONResponse({"error": "no upstream key succeeded", "tried": tried, "errors": errors}, status_code=502)


# -------------------------
# Admin endpoints
# -------------------------
def is_admin(auth_header: Optional[str]) -> bool:
    if not auth_header:
        return False
    if auth_header == ADMIN_TOKEN:
        return True
    low = auth_header.lower()
    if low.startswith("bearer "):
        return auth_header.split(" ", 1)[1] == ADMIN_TOKEN
    return False


@APP.get("/status")
async def status(x_proxy_admin: Optional[str] = Header(None)):
    if not is_admin(x_proxy_admin):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return JSONResponse({"keys": POOL.status()})


@APP.post("/reload-keys")
async def reload_keys(x_proxy_admin: Optional[str] = Header(None)):
    if not is_admin(x_proxy_admin):
        raise HTTPException(status_code=401, detail="Unauthorized")
    global KEYS_LIST, POOL
    KEYS_LIST = load_keys_from_file(KEYS_FILE)
    POOL = KeyPool(KEYS_LIST)
    return JSONResponse({"reloaded": True, "num_keys": len(KEYS_LIST)})


# -------------------------
# Run note:
# uvicorn main:APP --host 127.0.0.1 --port 8000
# -------------------------
