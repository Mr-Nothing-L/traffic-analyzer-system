"""OSD 站点信息抽取:视频首帧 → qwen 视觉理解 → {road, stake, direction, camera}。

[文件说明]
作用:cv2 读第 0 帧编码为 jpeg base64,经 OpenAI 兼容 chat/completions 让 qwen
只输出封闭 JSON(无 OSD 输出 null);结果按 stem 缓存到 osd_cache.json,命中不重调;
一切异常 → {"road": null, ...} 不抛出(异常结果不缓存,便于下次重试)。
上游:scripts/build_rag_index.py。
下游:http://$RAG_OSD_BASE_URL/chat/completions(缺省读 traffic_analyzer/config/.env
的 LLM_PROVIDER_0_*)。
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.request
from pathlib import Path

import cv2

_DEFAULT_BASE_URL = "http://10.103.0.6:8003/v1"
_DEFAULT_MODEL = "qwen3.8-27b-fp8"
_ENV_FILE = Path(__file__).resolve().parents[1] / "config" / ".env"
NULL_SITE = {"road": None, "stake": None, "direction": None, "camera": None}

_PROMPT = (
    "这是高速公路监控画面,左上角通常叠有 OSD 文字,包含道路、桩号、方向、摄像头编号,"
    '形如「北京-G3京台高速-道路 K18+470-进京-3」。'
    '只输出一个 JSON 对象:{"road": 道路, "stake": 桩号, "direction": 方向, "camera": 摄像头编号},'
    "不要输出任何其他内容;若画面中没有可辨认的 OSD 信息,输出 null。"
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _osd_config() -> tuple[str, str, str]:
    base = os.environ.get("RAG_OSD_BASE_URL")
    model = os.environ.get("RAG_OSD_MODEL")
    key = os.environ.get("RAG_OSD_API_KEY")
    if not (base and model and key):
        try:
            from dotenv import dotenv_values

            env = dotenv_values(_ENV_FILE)
        except Exception:  # noqa: BLE001
            env = {}
        base = base or env.get("LLM_PROVIDER_0_BASE_URL") or _DEFAULT_BASE_URL
        model = model or env.get("LLM_PROVIDER_0_MODEL") or _DEFAULT_MODEL
        key = key or env.get("LLM_PROVIDER_0_API_KEY") or ""
    return base.rstrip("/"), model, key


def _load_cache(cache_path: Path) -> dict:
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_cache(cache_path: Path, cache: dict) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _first_frame_jpeg_b64(video_path: Path) -> str:
    cap = cv2.VideoCapture(str(video_path))
    try:
        ok, frame = cap.read()
    finally:
        cap.release()
    if not ok or frame is None:
        raise RuntimeError(f"cannot read first frame: {video_path}")
    ok, buf = cv2.imencode(".jpg", frame)
    if not ok:
        raise RuntimeError(f"jpeg encode failed: {video_path}")
    return base64.b64encode(buf.tobytes()).decode()


def _parse_site_json(content: str) -> dict:
    """解析模型输出;null / 无 JSON → 全 None 站点;结构非法 → 抛异常。"""
    content = content.strip()
    if not content or content.startswith("null"):
        return dict(NULL_SITE)
    m = _JSON_RE.search(content)
    if not m:
        raise ValueError(f"no JSON in model output: {content[:100]}")
    data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError(f"unexpected model output: {content[:100]}")
    return {k: (data.get(k) or None) for k in NULL_SITE}


def _extract(video_path: Path) -> dict:
    base, model, key = _osd_config()
    b64 = _first_frame_jpeg_b64(video_path)
    payload = {
        "model": model,
        "max_tokens": 200,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    {"type": "text", "text": _PROMPT},
                ],
            }
        ],
    }
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.load(resp)
    content = data["choices"][0]["message"]["content"] or ""
    return _parse_site_json(content)


def extract_site(video_path, cache_path) -> dict:
    """抽取 OSD 站点信息;按 stem 缓存,命中不重调;异常返回全 None 不抛出。"""
    video_path = Path(video_path)
    cache_path = Path(cache_path)
    stem = video_path.stem
    cache = _load_cache(cache_path)
    if stem in cache:
        return cache[stem]
    try:
        site = _extract(video_path)
    except Exception:  # noqa: BLE001
        return dict(NULL_SITE)
    cache[stem] = site
    _save_cache(cache_path, cache)
    return site
