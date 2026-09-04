"""wemm-embedding-9b embedding 客户端(文本 / 视频字节)。

[文件说明]
作用:封装 OpenAI 兼容的 /v1/embeddings 调用;文本 input 为字符串,视频 input 为
base64 dataURL;超时 300s,最多 3 次指数退避重试,4xx 不重试。
上游:scripts/build_rag_index.py、scripts/rag_search.py。
下游:http://$WEMM_BASE_URL/embeddings(默认 http://10.103.0.6:8000/v1)。
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request

_DEFAULT_BASE_URL = "http://10.103.0.6:8000/v1"
_DEFAULT_API_KEY = "sk-traffic-27b-2026"
_DEFAULT_MODEL = "wemm-embedding-9b"
_TIMEOUT_S = 300
_MAX_ATTEMPTS = 3


def _config() -> tuple[str, str, str]:
    base = os.environ.get("WEMM_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")
    key = os.environ.get("WEMM_API_KEY", _DEFAULT_API_KEY)
    model = os.environ.get("WEMM_MODEL", _DEFAULT_MODEL)
    return base, key, model


def _post_embeddings(payload: dict) -> list[list[float]]:
    """POST /embeddings,返回各 input 对应的 embedding 列表;4xx 直接抛出。"""
    base, key, _ = _config()
    req = urllib.request.Request(
        f"{base}/embeddings",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    last_err: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
                data = json.load(resp)
            return [item["embedding"] for item in data["data"]]
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                raise
            last_err = e
        except Exception as e:  # noqa: BLE001
            last_err = e
        time.sleep(2 ** attempt)
    raise RuntimeError(f"embedding request failed after {_MAX_ATTEMPTS} attempts: {last_err}")


def embed_texts(texts: list[str]) -> list[list[float]]:
    """逐条文本 embedding(服务端 input 约定为字符串)。"""
    _, _, model = _config()
    return [_post_embeddings({"model": model, "input": t})[0] for t in texts]


def embed_video_bytes(data: bytes, ext: str = "mp4") -> list[float]:
    """视频字节 → 4096 维 L2 归一化向量。"""
    _, _, model = _config()
    b64 = base64.b64encode(data).decode()
    payload = {
        "model": model,
        "input": [{"type": "video_url", "video_url": {"url": f"data:video/{ext};base64,{b64}"}}],
    }
    return _post_embeddings(payload)[0]
