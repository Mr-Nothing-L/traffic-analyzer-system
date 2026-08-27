"""SFT 关键帧面板后端:候选帧、关键帧增删/排序、qwen 智能挑选与批量任务。

[文件说明]
作用:SFT 标注的 chunk 级关键帧(存于 analysis/<stem>/关键帧/,文件名
NN_t{sec}s.jpg,序号即时间顺序;真相源为 <stem>.json 的 keyframes 字段,
SftSample.keyframes)。提供 GET /api/videos/{stem}/keyframes/candidates
(首末帧间均匀 10 个候选)、GET/POST /api/results/{stem}/keyframes 与
DELETE .../{filename}、PUT .../order(增删排序即时落盘并回传新 file_sig)、
POST /api/videos/{stem}/keyframes/auto_pick(qwen3.8 从候选中挑 2-5 帧,
手动/推理后自动/批量三路径共用的核心;严格解析 {"pick":[i,...]},失败不改
现有关键帧)、POST /api/keyframes/batch + GET /api/keyframes/batch/{id}
(后台 daemon 线程逐个 auto_pick,状态仅存内存)。
schedule_after_infer 供 jobs/queue.py 在 infer 成功后延迟导入调用(无标注或
已有关键帧时静默跳过,异常仅 warning)。
LLM 调用按 .env 主用 provider 经 ConfigManager 解析,仅支持 OpenAI 兼容的
aliyun(DashScope compatible-mode,qwen-vl 系列):接入 VLMInferenceEngine
需 PromptTemplate/jinja2 并在 web 进程引入 anthropic/google SDK,成本过高;
anthropic/google 主用时抛 KeyframeError 由调用方优雅降级。openai/httpx
在函数内局部导入,避免 web 进程启动即拉起 SDK。
上游:web/app.py(include_router);web/jobs/queue.py(infer 成功钩子);
frontend KeyframePanel.vue 与 TreeToolbar 批量入口。
下游:web/frames.py(read_video_meta/read_frame_jpeg,复用其 LRU 缓存)、
web/workspace.py(require_workspace/validate_stem/find_video/analysis_dir/
invalidate_caches)、web/evidence(_read_json/_atomic_write_json/_file_sig 与
locks._put_locks 同一把 per-stem 锁,防止与 SFT 文本保存互踩)、
web/evidence_schema.py(SftKeyframe)、core/config_manager(.env provider 解析)。
"""

from __future__ import annotations

import base64
import copy
import json
import logging
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from traffic_analyzer.core.config_manager import ConfigManager
from traffic_analyzer.web import event_config
from traffic_analyzer.web import frames as frames_mod
from traffic_analyzer.web import workspace as workspace_mod
from traffic_analyzer.web.evidence import (
    _CorruptJsonError,
    _atomic_write_json,
    _file_sig,
    _read_json,
)
from traffic_analyzer.web.evidence.locks import _put_locks

logger = logging.getLogger(__name__)

router = APIRouter()

# analysis/<stem>/ 下的关键帧目录与命名契约(NN=两位序号承载顺序,sec 一位小数)
KEYFRAME_DIR_NAME = "关键帧"
_FILENAME_RE = re.compile(r"^\d{2}_t\d+(?:\.\d+)?s\.jpg$")
# 候选帧数量与挑选数量约束(设计文档:10 帧候选挑 2-5)
N_CANDIDATES = 10
MIN_PICK = 2
MAX_PICK = 5

_CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
_DASHSCOPE_COMPAT_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class _AddRequest(BaseModel):
    frame_index: int
    time_sec: float


class _OrderRequest(BaseModel):
    filenames: List[str]


class _AutoPickRequest(BaseModel):
    overwrite: bool = False


class _BatchRequest(BaseModel):
    stems: List[str]
    overwrite: bool = False


class KeyframeError(Exception):
    """智能挑选业务失败(auto_pick 路由映射 502,batch 记 failed)。"""


class KeyframeExistsError(KeyframeError):
    """已有关键帧且未允许覆盖(batch 记 skipped,auto_pick 路由映射 409)。"""


# ---------------------------------------------------------------------------
# 候选帧计算与文件名工具(路由外的纯函数,便于函数级冒烟测试)
# ---------------------------------------------------------------------------


def candidate_indices(frame_count: int, count: int = N_CANDIDATES) -> List[int]:
    """首帧与末帧之间均匀取 count 个索引;帧数不足 count 时返回全部帧。

    round 可能产生重复索引(frame_count 只比 count 大一点时),保序去重兜底。
    """
    if frame_count <= 0:
        return []
    if frame_count <= count:
        return list(range(frame_count))
    indices = [round(i * (frame_count - 1) / (count - 1)) for i in range(count)]
    return list(dict.fromkeys(indices))


def compute_candidates(meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    """视频元信息 → 候选列表 [{index, time_sec}](fps 不可用时时间记 0.0)。"""
    fps = float(meta.get("fps") or 0.0)
    out: List[Dict[str, Any]] = []
    for index in candidate_indices(int(meta["frame_count"])):
        out.append({"index": index, "time_sec": round(index / fps, 1) if fps > 0 else 0.0})
    return out


def kf_dir(workspace: Path, stem: str) -> Path:
    return workspace_mod.analysis_dir(workspace, stem) / KEYFRAME_DIR_NAME


def sft_json_path(workspace: Path, stem: str) -> Path:
    return workspace_mod.analysis_dir(workspace, stem) / f"{stem}.json"


def keyframe_filename(order: int, time_sec: float) -> str:
    return f"{order:02d}_t{time_sec:.1f}s.jpg"


def parse_auto_pick_response(raw: str, count: int) -> List[int]:
    """严格解析模型应答 {"pick": [编号,...]}。

    非 JSON/缺 pick 键/非整数项/越界均判失败;去重后数量必须落在
    MIN_PICK..MAX_PICK(调用方以失败处理,不改动现有关键帧)。
    """
    text = (raw or "").strip()
    obj: Any = None
    try:
        obj = json.loads(text)
    except ValueError:
        pass
    if obj is None:
        # 容忍模型把 JSON 包进散文字符里:截取首个 {...} 再试一次。
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                obj = json.loads(m.group(0))
            except ValueError:
                pass
    if not isinstance(obj, dict) or not isinstance(obj.get("pick"), list):
        raise ValueError('响应不是 JSON 对象或缺 "pick" 数组')
    picked: List[int] = []
    for v in obj["pick"]:
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError(f"pick 含非整数项:{v!r}")
        if not 0 <= v < count:
            raise ValueError(f"pick 编号越界:{v}(候选共 {count} 帧)")
        picked.append(v)
    deduped = list(dict.fromkeys(picked))
    if not MIN_PICK <= len(deduped) <= MAX_PICK:
        raise ValueError(f"pick 数量 {len(deduped)} 不在 {MIN_PICK}-{MAX_PICK}")
    return deduped


# ---------------------------------------------------------------------------
# 落盘原语:两阶段重排 + SFT JSON 同步(调用方持有 _put_locks[stem])
# ---------------------------------------------------------------------------


def _rewrite_dir(kf_directory: Path, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 items 给定顺序把目录重写为 NN_t{sec}s.jpg 连续编号,返回新条目列表。

    items 每项:frame_index / time_sec 必填;src 为被引用的旧文件名(重排),
    jpeg 为新增帧的字节(新落盘),二者互斥。先把现有 *.jpg 整体挪入
    ``.stage`` 暂存名再落最终名,规避新旧序号互相占用;未被引用的暂存文件
    (删除场景与孤儿残留)收尾统一清除。
    """
    kf_directory.mkdir(parents=True, exist_ok=True)
    staged: Dict[str, Path] = {}
    for i, path in enumerate(sorted(kf_directory.glob("*.jpg"))):
        tmp = kf_directory / f".stage{i:03d}.tmp"
        path.rename(tmp)
        staged[path.name] = tmp
    try:
        written: List[Dict[str, Any]] = []
        for order, item in enumerate(items):
            name = keyframe_filename(order, float(item["time_sec"]))
            dst = kf_directory / name
            src_name = item.get("src")
            src = staged.pop(src_name, None) if src_name is not None else None
            if src_name is not None and src is None:
                raise FileNotFoundError(f"keyframe file missing: {src_name}")
            if src is None:
                dst.write_bytes(item["jpeg"])  # type: ignore[typeddict-item]
            elif src != dst:
                src.rename(dst)
            written.append(
                {
                    "filename": name,
                    "frame_index": int(item["frame_index"]),
                    "time_sec": round(float(item["time_sec"]), 1),
                }
            )
        return written
    finally:
        for tmp in staged.values():
            tmp.unlink(missing_ok=True)


def _sync_sft_json(sft_path: Path, entries: List[Dict[str, Any]]) -> None:
    """把关键帧条目写回 <stem>.json 的 keyframes 字段(其余内容原样保留)。"""
    disk = _read_json(sft_path)
    if not isinstance(disk, dict):  # 调用方已保证存在,此处防御并发删除
        raise FileNotFoundError(str(sft_path))
    disk["keyframes"] = entries
    _atomic_write_json(sft_path, disk)


def _with_order(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{"order": i, **e} for i, e in enumerate(entries)]


def _read_keyframes_locked(sft_path: Path) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """锁内读磁盘 SFT JSON 及其 keyframes 条目(无标注 → (None, []))。"""
    try:
        disk = _read_json(sft_path)
    except _CorruptJsonError as exc:
        raise HTTPException(status_code=422, detail=f"Existing SFT file is corrupt: {exc}")
    entries = list(disk.get("keyframes") or []) if isinstance(disk, dict) else []
    return disk, entries


# ---------------------------------------------------------------------------
# 智能挑选核心(auto_pick 三路径共用)
# ---------------------------------------------------------------------------

_PROMPT_TEMPLATE = (
    "你是交通视频标注助手。下面按时间顺序给出同一段视频均匀采样的 {n} 张候选帧,"
    "依次编号 0-{last}。\n\n"
    "该视频的人工事件结论如下:\n{context}\n\n"
    "请从中选出 {minp} 到 {maxp} 帧,要求所选帧合起来覆盖视频中发生的所有事件"
    "(优先选事件主体清晰可见的帧,不要多选相似帧)。\n"
    '严格只输出一个 JSON 对象:"{{"pick": [编号, ...]}},不要输出任何其他文字。'
)

# openai/httpx 局部导入的理由见模块 docstring;ConfigManager 解析口径与
# web/llm_settings.py 相同(.env 行序,index 0 = 主用)。


def build_context_text(disk: Dict[str, Any], limit: int = 800) -> str:
    """从 SFT 样本提炼给模型的结论上下文:description 前 ~limit 字 + action 名。"""
    parts: List[str] = []
    names = event_config.event_name_index()
    inverted = {event_id: name for name, event_id in names.items()}
    actions = [a for a in disk.get("action") or [] if a in inverted]
    if actions:
        parts.append("事件类别:" + "、".join(inverted[a] for a in actions))
    description = str(disk.get("description") or "")
    if description:
        parts.append(description[:limit])
    return "\n\n".join(parts) if parts else "(样本暂无人工结论)"


def _primary_provider() -> Optional[Any]:
    try:
        providers = ConfigManager(str(_CONFIG_DIR))._load_env_llm_providers()
    except Exception as exc:  # .env 缺失/字段损坏:视为未配置而非崩 500
        logger.warning("resolve LLM provider for keyframe auto-pick failed: %s", exc)
        return None
    return providers[0] if providers else None


def _call_llm_pick(image_urls: List[str], context_text: str) -> str:
    """openai SDK 多图消息裸调主用 provider,返回原始应答文本。"""
    cfg = _primary_provider()
    if cfg is None:
        raise KeyframeError("未配置任何 LLM provider(.env),无法智能挑选")
    if cfg.provider != "aliyun":
        raise KeyframeError(
            f"主用 LLM provider 为「{cfg.provider}」,智能挑选需要 OpenAI 兼容的 "
            "aliyun(qwen-vl);请在 LLM 设置中将 aliyun 行置为主用后重试"
        )
    import httpx
    import openai

    http_client = httpx.Client(proxy=None, trust_env=False, timeout=float(getattr(cfg, "timeout", None) or 60))
    client = openai.OpenAI(api_key=cfg.api_key, base_url=cfg.base_url or _DASHSCOPE_COMPAT_URL, http_client=http_client)
    content: List[Dict[str, Any]] = [
        {
            "type": "text",
            "text": _PROMPT_TEMPLATE.format(
                n=len(image_urls),
                last=len(image_urls) - 1,
                context=context_text,
                minp=MIN_PICK,
                maxp=MAX_PICK,
            ),
        }
    ]
    content += [{"type": "image_url", "image_url": {"url": url}} for url in image_urls]
    try:
        resp = client.chat.completions.create(
            model=cfg.model,
            messages=[{"role": "user", "content": content}],
            temperature=0,
        )
        return resp.choices[0].message.content or ""
    finally:
        client.close()


def perform_auto_pick(
    workspace: Path, stem: str, *, overwrite: bool = False
) -> Tuple[List[int], List[Dict[str, Any]], Optional[str]]:
    """对一个 stem 运行智能挑选并落盘,返回 (选中的候选序号, 新条目列表, 新 file_sig)。

    失败(找不到视频/SFT/LLM 应答不合格等)抛 KeyframeError 且不动磁盘;
    已有关键帧且 overwrite=False 抛 KeyframeExistsError。SFT JSON 不存在抛
    KeyframeError(批量路径事先过滤,不依赖此分支)。
    """
    video = workspace_mod.find_video(workspace, stem)
    if video is None:
        raise KeyframeError(f"工作区中找不到视频:{stem}")
    sft_path = sft_json_path(workspace, stem)
    disk = _read_json(sft_path)
    if not isinstance(disk, dict):
        raise KeyframeError(f"该视频没有 SFT 标注:{stem}")
    existing = disk.get("keyframes") or []
    if existing and not overwrite:
        raise KeyframeExistsError(f"已有关键帧({len(existing)} 个)")

    meta = frames_mod.read_video_meta(video)
    if meta is None:
        raise KeyframeError(f"视频无法读取:{stem}")
    candidates = compute_candidates(meta)
    image_urls: List[str] = []
    valid: List[Dict[str, Any]] = []
    for cand in candidates:
        data = frames_mod.read_frame_jpeg(video, cand["index"])
        if data is None:  # 元信息帧数虚高时的兜底:跳过取不到的候选
            continue
        image_urls.append("data:image/jpeg;base64," + base64.b64encode(data).decode())
        valid.append(cand)
    if len(valid) < MIN_PICK:
        raise KeyframeError(f"可用候选帧不足 {MIN_PICK} 张")

    raw = _call_llm_pick(image_urls, build_context_text(disk))
    try:
        picks = parse_auto_pick_response(raw, len(valid))
    except ValueError as exc:
        raise KeyframeError(f"模型应答不合格({exc})") from exc

    items: List[Dict[str, Any]] = []
    for i in picks:  # 取真实 JPEG 字节(刚抽过,LRU 必命中;仍兜底 None)
        data = frames_mod.read_frame_jpeg(video, valid[i]["index"])
        if data is None:
            raise KeyframeError(f"候选帧 {valid[i]['index']} 抽取失败")
        items.append({"frame_index": valid[i]["index"], "time_sec": valid[i]["time_sec"], "jpeg": data})
    items.sort(key=lambda it: float(it["time_sec"]))  # 落盘顺序 = 时间顺序
    with _put_locks[stem]:
        # 二次确认防竞态:LLM 调用期间用户可能已手工加帧且未允许覆盖。
        _, current = _read_keyframes_locked(sft_path)
        if current and not overwrite:
            raise KeyframeExistsError(f"已有关键帧({len(current)} 个)")
        written = _rewrite_dir(kf_dir(workspace, stem), items)
        _sync_sft_json(sft_path, written)
        sig = _file_sig(sft_path)
    workspace_mod.invalidate_caches()
    logger.info("keyframe auto-pick: %s <- candidates %s", stem, picks)
    return picks, written, sig


def schedule_after_infer(workspace: Path, stem: str) -> None:
    """推理成功后的自动挑选(daemon 线程):无标注/已有关键帧直接跳过。"""
    def _work() -> None:
        try:
            if not sft_json_path(workspace, stem).is_file():
                logger.info("keyframe auto-pick skipped(no SFT json): %s", stem)
                return
            perform_auto_pick(workspace, stem, overwrite=False)
        except Exception as exc:  # 自动路径绝不影响任务状态,一律只记日志
            logger.warning("keyframe auto-pick failed for %s: %s", stem, exc)

    threading.Thread(target=_work, name=f"keyframes-auto-{stem}", daemon=True).start()


# ---------------------------------------------------------------------------
# 批量任务(内存状态,不持久化)
# ---------------------------------------------------------------------------

_batch_lock = threading.Lock()
_batches: Dict[str, Dict[str, Any]] = {}
_MAX_BATCHES_KEPT = 20


def batch_snapshot(batch_id: str) -> Optional[Dict[str, Any]]:
    with _batch_lock:
        state = _batches.get(batch_id)
        if state is None:
            return None
        snapshot = copy.deepcopy(state)
    snapshot["running"] = any(
        item["status"] == "running" for item in snapshot["items"].values()
    )
    return snapshot


def _batch_item(batch_id: str, stem: str, status: str, message: str = "") -> None:
    with _batch_lock:
        state = _batches.get(batch_id)
        if state is None:
            return
        state["items"][stem] = {"status": status, "message": message}
        state["finished"] += 1


def _run_batch(workspace: Path, stems: List[str], overwrite: bool, batch_id: str) -> None:
    for stem in stems:
        adir = workspace_mod.analysis_dir(workspace, stem)
        if not (adir / f"{stem}.json").is_file():
            _batch_item(batch_id, stem, "skipped", "无 SFT 标注")
            continue
        try:
            current = _read_json(sft_json_path(workspace, stem)) or {}
            if (current.get("keyframes") or []) and not overwrite:
                _batch_item(batch_id, stem, "skipped", "已有关键帧")
                continue
        except _CorruptJsonError as exc:
            _batch_item(batch_id, stem, "failed", f"SFT 文件损坏:{exc}")
            continue
        _batch_item_running(batch_id, stem)
        try:
            perform_auto_pick(workspace, stem, overwrite=overwrite)
            _batch_item(batch_id, stem, "ok")
        except KeyframeExistsError as exc:
            _batch_item(batch_id, stem, "skipped", str(exc))
        except Exception as exc:
            logger.warning("batch keyframe auto-pick failed for %s: %s", stem, exc)
            _batch_item(batch_id, stem, "failed", str(exc))


def _batch_item_running(batch_id: str, stem: str) -> None:
    """running 态不计入 finished(终态计数),单独置位。"""
    with _batch_lock:
        state = _batches.get(batch_id)
        if state is not None:
            state["items"][stem] = {"status": "running", "message": ""}


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


def _video_or_404(request: Request, stem: str) -> Path:
    workspace = workspace_mod.require_workspace(request)
    workspace_mod.validate_stem(stem)
    video = workspace_mod.find_video(workspace, stem)
    if video is None:
        raise HTTPException(status_code=404, detail="Video not found")
    return video


def _require_sft_stem(request: Request, stem: str) -> Path:
    workspace = workspace_mod.require_workspace(request)
    workspace_mod.validate_stem(stem)
    return workspace


@router.get("/api/videos/{stem}/keyframes/candidates")
def get_candidates(stem: str, request: Request) -> List[Dict[str, Any]]:
    """首末帧之间均匀采样的候选帧 [{index, time_sec}](≤10 个)。"""
    video = _video_or_404(request, stem)
    meta = frames_mod.read_video_meta(video)
    if meta is None:
        raise HTTPException(status_code=404, detail="Video metadata unreadable")
    return compute_candidates(meta)


@router.get("/api/results/{stem}/keyframes")
def list_keyframes(stem: str, request: Request) -> List[Dict[str, Any]]:
    workspace = _require_sft_stem(request, stem)
    sft_path = sft_json_path(workspace, stem)
    _, entries = _read_keyframes_locked(sft_path)  # 读也走锁:避开半写窗口
    return _with_order(entries)


@router.post("/api/results/{stem}/keyframes")
def add_keyframe(stem: str, body: _AddRequest, request: Request) -> Dict[str, Any]:
    workspace = _require_sft_stem(request, stem)
    video = _video_or_404(request, stem)
    try:
        jpeg = frames_mod.read_frame_jpeg(video, body.frame_index)
    except OSError:
        jpeg = None
    if jpeg is None:
        raise HTTPException(status_code=404, detail="Frame index out of range")
    time_sec = round(body.time_sec, 1)
    sft_path = sft_json_path(workspace, stem)
    directory = kf_dir(workspace, stem)
    with _put_locks[stem]:
        disk, existing = _read_keyframes_locked(sft_path)
        if disk is None:
            raise HTTPException(status_code=404, detail="SFT file not found")
        if any(e.get("frame_index") == body.frame_index for e in existing):
            # 幂等:该候选帧已是关键帧(hover 双击)直接回现状,不重复落盘
            return {"keyframes": _with_order(existing), "file_sig": _file_sig(sft_path)}
        # 时间顺序由稳定排序承载:已在条目保持相对顺序,新帧按秒插入
        items = [{**e, "src": e["filename"]} for e in existing]
        items.append({"frame_index": body.frame_index, "time_sec": time_sec, "jpeg": jpeg})
        items.sort(key=lambda it: float(it["time_sec"]))
        written = _rewrite_dir(directory, items)
        _sync_sft_json(sft_path, written)
        sig = _file_sig(sft_path)
    workspace_mod.invalidate_caches()
    return {"keyframes": _with_order(written), "file_sig": sig}


@router.delete("/api/results/{stem}/keyframes/{filename}")
def delete_keyframe(stem: str, filename: str, request: Request) -> Dict[str, Any]:
    workspace = _require_sft_stem(request, stem)
    if not _FILENAME_RE.match(filename):
        raise HTTPException(status_code=404, detail="Unknown keyframe")
    sft_path = sft_json_path(workspace, stem)
    with _put_locks[stem]:
        disk, existing = _read_keyframes_locked(sft_path)
        if disk is None:
            raise HTTPException(status_code=404, detail="SFT file not found")
        remaining = [e for e in existing if e.get("filename") != filename]
        if len(remaining) == len(existing):
            raise HTTPException(status_code=404, detail="Unknown keyframe")
        items = [{**e, "src": e["filename"]} for e in remaining]
        written = _rewrite_dir(kf_dir(workspace, stem), items)
        _sync_sft_json(sft_path, written)
        sig = _file_sig(sft_path)
    workspace_mod.invalidate_caches()
    return {"keyframes": _with_order(written), "file_sig": sig}


@router.put("/api/results/{stem}/keyframes/order")
def reorder_keyframes(stem: str, body: _OrderRequest, request: Request) -> Dict[str, Any]:
    workspace = _require_sft_stem(request, stem)
    sft_path = sft_json_path(workspace, stem)
    with _put_locks[stem]:
        disk, existing = _read_keyframes_locked(sft_path)
        if disk is None:
            raise HTTPException(status_code=404, detail="SFT file not found")
        by_name = {e["filename"]: e for e in existing}
        if sorted(body.filenames) != sorted(by_name):
            raise HTTPException(status_code=422, detail="filenames 与现有关键帧不一致")
        items = [{**by_name[name], "src": name} for name in body.filenames]
        written = _rewrite_dir(kf_dir(workspace, stem), items)
        _sync_sft_json(sft_path, written)
        sig = _file_sig(sft_path)
    workspace_mod.invalidate_caches()
    return {"keyframes": _with_order(written), "file_sig": sig}


@router.post("/api/videos/{stem}/keyframes/auto_pick")
def auto_pick(stem: str, request: Request, body: Optional[_AutoPickRequest] = None) -> Dict[str, Any]:
    workspace = _require_sft_stem(request, stem)
    overwrite = bool(body and body.overwrite)
    try:
        picks, written, sig = perform_auto_pick(workspace, stem, overwrite=overwrite)
    except KeyframeExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except KeyframeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except _CorruptJsonError as exc:
        raise HTTPException(status_code=422, detail=f"Existing SFT file is corrupt: {exc}")
    return {"picked": picks, "keyframes": _with_order(written), "file_sig": sig}


@router.post("/api/keyframes/batch")
def start_batch(body: _BatchRequest, request: Request) -> Dict[str, Any]:
    if not body.stems:
        raise HTTPException(status_code=422, detail="stems 不能为空")
    workspace = workspace_mod.require_workspace(request)
    for stem in body.stems:
        workspace_mod.validate_stem(stem)
    batch_id = uuid.uuid4().hex[:12]
    state: Dict[str, Any] = {
        "id": batch_id,
        "total": len(body.stems),
        "finished": 0,
        "items": {s: {"status": "pending", "message": ""} for s in body.stems},
    }
    with _batch_lock:
        # 只保留最近 N 批,防长驻进程内存无限增长
        while len(_batches) >= _MAX_BATCHES_KEPT:
            _batches.pop(next(iter(_batches)))
        _batches[batch_id] = state
    threading.Thread(
        target=_run_batch,
        args=(workspace, list(body.stems), body.overwrite, batch_id),
        daemon=True,
        name=f"keyframes-batch-{batch_id}",
    ).start()
    return {"id": batch_id}


@router.get("/api/keyframes/batch/{batch_id}")
def get_batch(batch_id: str, request: Request) -> Dict[str, Any]:
    snapshot = batch_snapshot(batch_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Unknown batch id")
    return snapshot
