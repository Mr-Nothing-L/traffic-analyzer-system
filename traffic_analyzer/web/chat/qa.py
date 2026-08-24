"""Quick-chat QA orchestration: intent classify, context gather, streaming, boxes.

[文件说明]
作用:快速对话问答编排。ask(ip, question) 为 SSE 事件生成器:maybe_compact
(超 COMPACT_AT 时用主用 provider 非流式压缩旧消息为中文摘要)→ classify
(主用 provider 非流式判定 event_analysis|content_query|chitchat,失败兜底
content_query)→ 按信源组装上下文(workspace_video: 均匀采样≤12帧,证据帧
优先,analysis/<stem>/<stem>_evidence.json 的 detected 事件摘要注入系统
prompt;upload_video 同抽帧;upload_images ≤8 张;none/chitchat 不带图)→
历史(summary + 近10轮文本)→ 流式调用(provider 按 .env 顺序 failover,
首块前失败切下一个,流式中失败 yield error 结束)→ event_analysis 时解析
尾部 ```json boxes 块并用 PIL 画框落盘(output/chat_uploads/<sha1(ip)[:12]>/)→
落库(user 在流前,assistant 在流后)。SDK 客户端构建参照
core/vlm_engine.py:_init_client_for_provider(httpx 绕开系统代理);
aliyun 流式 chunk 的 delta 可能带 reasoning_content(作为 think 事件)。
上游:web/chat/routes.py(POST /api/chat/ask)。
下游:web/chat/store.py、tokens.py、paths.py;web/frames.py(抽帧);
utils/bbox_geometry.py、utils/image_drawing.py(画框);
core/config_manager.py(provider 列表);anthropic/openai/google SDK(惰性导入)。
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import re
import uuid
import zlib
from pathlib import Path
from typing import Any, Dict, Generator, Iterable, List, Optional, Tuple

import httpx

from traffic_analyzer.core.config_manager import ConfigManager
from traffic_analyzer.models.config import LLMProviderConfig
from traffic_analyzer.utils.bbox_geometry import _norm_to_px
from traffic_analyzer.utils.image_drawing import (
    _draw_text_with_background,
    _load_scaled_font,
)
from traffic_analyzer.web.chat import paths, store, tokens
from traffic_analyzer.web.frames import read_frame_jpeg, read_video_meta

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"

_MAX_VIDEO_FRAMES = 12
_MAX_UPLOAD_IMAGES = 8
_HISTORY_TURNS = 10  # 近 10 轮 user/assistant 文本
_EVIDENCE_SUMMARY_CHARS = 2000

# 画框颜色调色板,按 label 哈希取色(同 label 恒同色)。
_PALETTE = (
    (255, 56, 56),
    (255, 157, 46),
    (46, 204, 113),
    (52, 152, 219),
    (155, 89, 182),
    (26, 188, 156),
    (241, 196, 15),
    (230, 126, 34),
)

_SYSTEM_BASE = (
    "你是高速公路交通事件分析助手。基于用户提供的视频抽帧/图片与事件证据摘要回答,"
    "使用中文,简洁、基于画面事实;无法从画面确认时明确说明,不要编造。"
)

_BOX_INSTRUCTION = (
    "回答正文结束后,若涉及事件/目标定位,输出一个 ```json 围栏块,格式:"
    '{"boxes":[{"frame_index": 图中序号(从0), "bbox_norm": [x1,y1,x2,y2], "label": "简短标签"}]}'
    ";bbox_norm 为 0-1 归一化坐标。无定位需要则不输出该块。"
)

_CLASSIFY_SYSTEM = (
    "判断用户问题属于哪一类,只回复 JSON:{\"intent\":\"...\"}。"
    "event_analysis=涉及交通事件检测/定位/框出目标;"
    "content_query=画面内容一般问答;chitchat=与画面/交通无关的闲聊。"
)

_COMPACT_SYSTEM = (
    "你是对话压缩器。把「已有摘要 + 旧对话」压缩为中文要点摘要,"
    "保留事件/时间/结论等关键信息,只输出摘要本身。"
)


# ---------------------------------------------------------------------------
# Provider loading / clients
# ---------------------------------------------------------------------------


def load_providers() -> List[LLMProviderConfig]:
    """Providers from .env (index 0 = primary, AUTO_SWITCH/ENABLED applied)."""
    try:
        mgr = ConfigManager(str(_CONFIG_DIR))
        return mgr._load_env_llm_providers()
    except Exception as exc:
        logger.warning("[chat] load providers failed: %s", exc)
        return []


def _client_for(cfg: LLMProviderConfig) -> Any:
    """Build the SDK client for one provider (mirrors vlm_engine's version)."""
    provider = cfg.provider.lower().strip()
    http_client = httpx.Client(proxy=None, trust_env=False, timeout=cfg.timeout)
    if provider == "anthropic":
        import anthropic

        kwargs: Dict[str, Any] = {"api_key": cfg.api_key, "http_client": http_client}
        if cfg.base_url:
            kwargs["base_url"] = cfg.base_url
        return anthropic.Anthropic(**kwargs)
    if provider == "google":
        import google.generativeai as genai

        genai.configure(api_key=cfg.api_key)
        return genai
    if provider == "aliyun":
        import openai

        base_url = cfg.base_url or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        return openai.OpenAI(
            api_key=cfg.api_key, base_url=base_url, http_client=http_client
        )
    raise ValueError(f"unsupported provider: {cfg.provider}")


# ---------------------------------------------------------------------------
# Non-streaming helpers (classify / compact)
# ---------------------------------------------------------------------------


def _complete(
    client: Any,
    cfg: LLMProviderConfig,
    system: str,
    user: str,
    max_tokens: int,
    temperature: float,
) -> str:
    """One non-streaming text completion against any supported provider."""
    provider = cfg.provider.lower().strip()
    if provider in ("aliyun",):
        resp = client.chat.completions.create(
            model=cfg.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return resp.choices[0].message.content or ""
    if provider == "anthropic":
        resp = client.messages.create(
            model=cfg.model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(
            getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text"
        )
    if provider == "google":
        model = client.GenerativeModel(cfg.model)
        resp = model.generate_content(
            [system, user],
            generation_config={"max_output_tokens": max_tokens, "temperature": temperature},
        )
        return getattr(resp, "text", "") or ""
    raise ValueError(f"unsupported provider: {cfg.provider}")


def classify(question: str, providers: List[LLMProviderConfig]) -> str:
    """Classify the question intent; any failure falls back to content_query."""
    if not providers:
        return "content_query"
    cfg = providers[0]
    try:
        client = _client_for(cfg)
        # max_tokens must cover the model's JSON-fence wrapper and any lead-in;
        # 50 truncates before the intent word (observed with local qwen server).
        text = _complete(
            client, cfg, _CLASSIFY_SYSTEM, question, max_tokens=500, temperature=0.0
        )
        match = re.search(r"(event_analysis|content_query|chitchat)", text or "")
        if match:
            return match.group(1)
        logger.warning("[chat] classify unparseable response %r; fallback", text)
    except Exception as exc:
        logger.warning("[chat] classify failed (%s); fallback content_query", exc)
    return "content_query"


# ---------------------------------------------------------------------------
# Streaming (one generator per provider; yields (kind, text))
# ---------------------------------------------------------------------------


def _openai_messages(
    messages: List[Dict[str, str]], images: List[bytes]
) -> List[Dict[str, Any]]:
    """OpenAI-style messages with images attached to the last user message."""
    if not images:
        return [dict(m) for m in messages]
    out = [dict(m) for m in messages]
    content: List[Dict[str, Any]] = [{"type": "text", "text": out[-1]["content"]}]
    for data in images:
        b64 = base64.b64encode(data).decode("ascii")
        content.append(
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
        )
    out[-1]["content"] = content
    return out


def _stream_aliyun(
    client: Any,
    cfg: LLMProviderConfig,
    messages: List[Dict[str, str]],
    images: List[bytes],
) -> Iterable[Tuple[str, str]]:
    stream = client.chat.completions.create(
        model=cfg.model,
        messages=_openai_messages(messages, images),
        max_tokens=cfg.max_tokens,
        temperature=cfg.temperature,
        stream=True,
    )
    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        # vLLM with --reasoning-parser emits think as `reasoning`; OpenAI-compat
        # convention is `reasoning_content`. Accept both.
        think = getattr(delta, "reasoning_content", None) or getattr(
            delta, "reasoning", None
        )
        if think:
            yield ("think", think)
        text = getattr(delta, "content", None)
        if text:
            yield ("delta", text)


def _stream_anthropic(
    client: Any,
    cfg: LLMProviderConfig,
    messages: List[Dict[str, str]],
    images: List[bytes],
) -> Iterable[Tuple[str, str]]:
    system = "\n".join(m["content"] for m in messages if m["role"] == "system")
    anth_msgs: List[Dict[str, Any]] = [
        {"role": m["role"], "content": m["content"]}
        for m in messages
        if m["role"] != "system"
    ]
    if images and anth_msgs:
        blocks: List[Dict[str, Any]] = [
            {"type": "text", "text": anth_msgs[-1]["content"]}
        ]
        for data in images:
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.b64encode(data).decode("ascii"),
                    },
                }
            )
        anth_msgs[-1] = {"role": "user", "content": blocks}
    kwargs: Dict[str, Any] = {
        "model": cfg.model,
        "max_tokens": cfg.max_tokens,
        "temperature": cfg.temperature,
        "messages": anth_msgs,
    }
    if system:
        kwargs["system"] = system
    with client.messages.stream(**kwargs) as stream:
        for event in stream:
            if getattr(event, "type", None) != "content_block_delta":
                continue
            delta = event.delta
            dtype = getattr(delta, "type", None)
            if dtype == "text_delta":
                yield ("delta", delta.text)
            elif dtype == "thinking_delta":
                yield ("think", delta.thinking)


def _stream_google(
    client: Any,
    cfg: LLMProviderConfig,
    messages: List[Dict[str, str]],
    images: List[bytes],
) -> Iterable[Tuple[str, str]]:
    from PIL import Image

    system = "\n".join(m["content"] for m in messages if m["role"] == "system")
    transcript = "\n".join(
        f"{'用户' if m['role'] == 'user' else '助手'}: {m['content']}"
        for m in messages
        if m["role"] != "system"
    )
    contents: List[Any] = []
    if system:
        contents.append(system)
    contents.append(transcript)
    contents.extend(Image.open(io.BytesIO(data)) for data in images)
    model = client.GenerativeModel(cfg.model)
    stream = model.generate_content(
        contents,
        generation_config={
            "max_output_tokens": cfg.max_tokens,
            "temperature": cfg.temperature,
        },
        stream=True,
    )
    for chunk in stream:
        text = getattr(chunk, "text", None)
        if text:
            yield ("delta", text)


def _stream_for(
    cfg: LLMProviderConfig,
    client: Any,
    messages: List[Dict[str, str]],
    images: List[bytes],
) -> Iterable[Tuple[str, str]]:
    provider = cfg.provider.lower().strip()
    if provider == "aliyun":
        return _stream_aliyun(client, cfg, messages, images)
    if provider == "anthropic":
        return _stream_anthropic(client, cfg, messages, images)
    if provider == "google":
        return _stream_google(client, cfg, messages, images)
    raise ValueError(f"unsupported provider: {cfg.provider}")


# ---------------------------------------------------------------------------
# Context gathering
# ---------------------------------------------------------------------------


def _uniform_indices(frame_count: int, limit: int) -> List[int]:
    if frame_count <= 0:
        return []
    if frame_count <= limit:
        return list(range(frame_count))
    step = frame_count / limit
    return sorted({min(frame_count - 1, int(i * step)) for i in range(limit)})


def _evidence_summary(evidence_path: Path) -> Tuple[str, List[int]]:
    """Detected-event summary (truncated) plus evidence frame indices."""
    try:
        data = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("[chat] evidence unreadable %s: %s", evidence_path, exc)
        return "", []
    lines: List[str] = []
    frames: List[int] = []
    for event in data.get("events") or []:
        if not event.get("detected"):
            continue
        regions = event.get("evidence_regions") or []
        fidx = sorted(
            {r["frame_index"] for r in regions if isinstance(r.get("frame_index"), int)}
        )
        frames.extend(fidx)
        label = str(regions[0].get("label", ""))[:120] if regions else ""
        lines.append(
            f"- 事件{event.get('event_id')}: {event.get('name')}"
            f"(证据帧: {fidx or '无'}) {label}".rstrip()
        )
    return "\n".join(lines)[:_EVIDENCE_SUMMARY_CHARS], frames


def _sample_video_frames(
    video_path: Path, priority_frames: List[int]
) -> List[bytes]:
    """Uniform ≤12 frames; evidence frames are kept preferentially."""
    meta = read_video_meta(video_path)
    if meta is None:
        logger.warning("[chat] video metadata unreadable: %s", video_path)
        return []
    frame_count = int(meta["frame_count"])
    uniform = _uniform_indices(frame_count, _MAX_VIDEO_FRAMES)
    priority = sorted({i for i in priority_frames if 0 <= i < frame_count})
    chosen = priority[: _MAX_VIDEO_FRAMES]
    for idx in uniform:
        if len(chosen) >= _MAX_VIDEO_FRAMES:
            break
        if idx not in chosen:
            chosen.append(idx)
    images: List[bytes] = []
    for idx in sorted(chosen):
        data = read_frame_jpeg(video_path, idx)
        if data is not None:
            images.append(data)
    return images


def _gather_context(state: Dict[str, Any], intent: str) -> Tuple[List[bytes], str]:
    """(images, evidence_summary) for the current source.

    chitchat only skips images when no source is set; with an active source the
    user expects the model to see it regardless of intent (local model, cheap).
    """
    kind = state.get("source_kind")
    if intent == "chitchat" and not kind:
        return [], ""
    ref = state.get("source_ref") or {}
    try:
        if kind == "workspace_video":
            video = Path(ref.get("path") or "")
            stem = ref.get("stem") or video.stem
            summary, eframes = "", []
            # 工作区 = 视频绝对路径去掉 rel 部分;证据在 analysis/<stem>/ 下。
            rel = ref.get("rel") or ""
            workspace = video
            for _ in Path(rel).parts:
                workspace = workspace.parent
            evidence_path = workspace / "analysis" / stem / f"{stem}_evidence.json"
            if evidence_path.is_file():
                summary, eframes = _evidence_summary(evidence_path)
            return _sample_video_frames(video, eframes), summary
        if kind == "upload_video":
            files = ref.get("files") or []
            if not files:
                return [], ""
            return _sample_video_frames(Path(files[0]), []), ""
        if kind == "upload_images":
            images: List[bytes] = []
            for f in (ref.get("files") or [])[:_MAX_UPLOAD_IMAGES]:
                try:
                    images.append(Path(f).read_bytes())
                except OSError as exc:
                    logger.warning("[chat] uploaded image unreadable %s: %s", f, exc)
            return images, ""
    except Exception as exc:
        logger.warning("[chat] gather context failed (%s); continue without", exc)
    return [], ""


def _build_system(
    intent: str,
    state: Dict[str, Any],
    evidence_summary: str,
    n_images: int,
) -> str:
    parts = [_SYSTEM_BASE]
    if state.get("summary"):
        parts.append(f"此前对话摘要:\n{state['summary']}")
    if evidence_summary:
        parts.append(f"已检测到的事件证据摘要:\n{evidence_summary}")
    if n_images:
        parts.append(f"随问题附带 {n_images} 张图片,按发送顺序编号 0..{n_images - 1}。")
    if intent == "event_analysis":
        parts.append(_BOX_INSTRUCTION)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Boxes parsing + annotation
# ---------------------------------------------------------------------------

_JSON_FENCE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_boxes(text: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Strip the trailing ```json boxes block; tolerate its absence/garbage."""
    matches = list(_JSON_FENCE.finditer(text or ""))
    if not matches:
        return text, []
    match = matches[-1]
    try:
        payload = json.loads(match.group(1))
        boxes = payload.get("boxes")
        if not isinstance(boxes, list):
            boxes = []
    except ValueError:
        boxes = []
    display = (text[: match.start()] + text[match.end() :]).strip()
    return display, [b for b in boxes if isinstance(b, dict)]


def _annotate_frames(
    ip: str, boxes: List[Dict[str, Any]], frames: List[bytes]
) -> List[str]:
    """Draw boxes on the referenced frames; return upload-dir-relative names."""
    from PIL import Image, ImageDraw

    by_frame: Dict[int, List[Dict[str, Any]]] = {}
    for box in boxes:
        idx = box.get("frame_index")
        bbox = box.get("bbox_norm")
        if (
            isinstance(idx, int)
            and 0 <= idx < len(frames)
            and isinstance(bbox, (list, tuple))
            and len(bbox) == 4
        ):
            by_frame.setdefault(idx, []).append(box)
    if not by_frame:
        return []

    sub = hashlib.sha1(ip.encode("utf-8")).hexdigest()[:12]
    outdir = paths.UPLOAD_DIR / sub
    outdir.mkdir(parents=True, exist_ok=True)
    uid = uuid.uuid4().hex[:12]
    font = _load_scaled_font(16)
    names: List[str] = []
    for i, frame_index in enumerate(sorted(by_frame)):
        try:
            img = Image.open(io.BytesIO(frames[frame_index])).convert("RGB")
        except Exception as exc:
            logger.warning("[chat] frame %s undecodable: %s", frame_index, exc)
            continue
        width, height = img.size
        draw = ImageDraw.Draw(img)
        for box in by_frame[frame_index]:
            label = str(box.get("label") or "target")[:40]
            color = _PALETTE[zlib.crc32(label.encode("utf-8")) % len(_PALETTE)]
            px = _norm_to_px([float(v) for v in box["bbox_norm"]], width, height)
            px = [
                max(0, min(width - 1, px[0])),
                max(0, min(height - 1, px[1])),
                max(0, min(width - 1, px[2])),
                max(0, min(height - 1, px[3])),
            ]
            draw.rectangle(px, outline=color, width=3)
            _draw_text_with_background(
                draw, label, (px[0], max(0, px[1] - 18)), background=color, font=font
            )
        name = f"{sub}/annotated_{uid}_{i}.jpg"
        img.save(outdir / f"annotated_{uid}_{i}.jpg", "JPEG", quality=90)
        names.append(name)
    return names


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------


def maybe_compact(ip: str) -> None:
    """Summarize old messages into chat_state.summary past COMPACT_AT tokens."""
    state = store.get_state(ip)
    messages = store.list_messages(ip)
    if not messages:
        return
    texts = [state.get("summary") or ""]
    for m in messages:
        texts.append(m["content"])
        texts.append(m.get("think") or "")
    est = tokens.estimate_request(texts, _MAX_VIDEO_FRAMES)
    if est <= tokens.COMPACT_AT:
        return
    keep = _HISTORY_TURNS * 2
    old = messages[:-keep] if len(messages) > keep else []
    if not old:
        return
    providers = load_providers()
    if not providers:
        return
    transcript = "\n".join(
        f"{m['role']}: {m['content']}" for m in old if m["role"] != "divider"
    )
    material = f"已有摘要:\n{state.get('summary') or '(无)'}\n\n旧对话:\n{transcript}"
    try:
        cfg = providers[0]
        client = _client_for(cfg)
        summary = _complete(
            client, cfg, _COMPACT_SYSTEM, material[:20000], max_tokens=1024, temperature=0.2
        )
        if summary.strip():
            store.set_summary(ip, summary.strip())
            store.delete_messages_up_to(ip, old[-1]["id"])
            logger.info("[chat] compacted %d messages for %s", len(old), ip)
    except Exception as exc:
        # 压缩失败不阻塞问答,下轮再试。
        logger.warning("[chat] compact failed (skipped): %s", exc)


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


def ask(
    ip: str, question: str, attachments: Tuple[str, ...] = ()
) -> Generator[Dict[str, Any], None, None]:
    """SSE event dicts for one question: think/delta/images/done or error.

    ``attachments`` = upload-dir-relative names (already validated by the
    route); persisted on the user message so bubbles can render them.
    """
    try:
        maybe_compact(ip)
    except Exception as exc:  # 压缩是优化,绝不阻塞问答
        logger.warning("[chat] maybe_compact error (skipped): %s", exc)

    providers = load_providers()
    if not providers:
        yield {"type": "error", "message": "未配置 LLM provider,请先在设置中配置"}
        return

    intent = classify(question, providers)
    state = store.get_state(ip)
    images, evidence_summary = _gather_context(state, intent)
    system = _build_system(intent, state, evidence_summary, len(images))

    messages: List[Dict[str, str]] = [{"role": "system", "content": system}]
    history = [m for m in store.list_messages(ip) if m["role"] in ("user", "assistant")]
    for m in history[-_HISTORY_TURNS * 2 :]:
        messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": question})

    store.add_message(ip, "user", question, images=tuple(attachments))

    full_text = ""
    think_text = ""
    emitted = False
    served = False
    last_exc: Optional[Exception] = None
    for cfg in providers:
        try:
            client = _client_for(cfg)
            for kind, text in _stream_for(cfg, client, messages, images):
                emitted = True
                if kind == "think":
                    think_text += text
                else:
                    full_text += text
                yield {"type": kind, "text": text}
            served = True
            break
        except Exception as exc:
            last_exc = exc
            if emitted:
                logger.warning("[chat] stream interrupted (%s)", exc)
                yield {"type": "error", "message": f"流式输出中断: {exc}"}
                return
            logger.warning(
                "[chat] provider %s failed before first chunk, trying next: %s",
                cfg.provider,
                exc,
            )
    if not served:
        yield {"type": "error", "message": f"所有 provider 调用失败: {last_exc}"}
        return

    display_text = full_text
    image_names: List[str] = []
    if intent == "event_analysis":
        display_text, boxes = _extract_boxes(full_text)
        if boxes:
            try:
                image_names = _annotate_frames(ip, boxes, images)
            except Exception as exc:
                logger.warning("[chat] annotate failed: %s", exc)
            if image_names:
                yield {
                    "type": "images",
                    "urls": [f"/api/chat/files/{n}" for n in image_names],
                }

    store.add_message(
        ip, "assistant", display_text, think=think_text, images=tuple(image_names)
    )
    # Carry the cleaned display text so clients can replace the raw stream
    # (which included the ```json boxes fence) with the stripped version.
    yield {"type": "done", "text": display_text}
