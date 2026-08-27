"""Video discovery and the /api/workspace/videos route.

[文件说明]
作用:工作区视频发现(list_videos,任意深度,跳过点目录与 PRUNED_DIR_NAMES
产出目录)及其进程内缓存版 list_videos_cached(key=workspace 路径,长 TTL +
主动失效,见 _VIDEOS_CACHE_TTL_SEC;/api/workspace/videos 与 dashboard 聚合
共用;失效统一走 invalidate_caches:workspace 变更 / infer 完成 / review /
SFT / 证据 PUT),GET /api/workspace/videos 路由,以及分析报告删除路由
(DELETE /api/workspace/analysis/{stem} 与批量 POST
/api/workspace/analysis/delete:删 analysis/<stem>/ 整目录,幂等,删后失效
缓存使「已完成」徽标随之消失)。大工作区不自动加载:videos/tree 均为前端
显式请求时才计算。
上游:web/workspace/core.py(缓存设施、has_results、require_workspace、
validate_stem、analysis_dir、VIDEO_EXTENSIONS、router);
web/workspace/__init__.py(聚合导出)。
下游:web/dashboard.py(看板聚合复用 list_videos_cached 与 _LongTTLCache)。
"""

from __future__ import annotations

import copy as copy_module
import os
import shutil
import stat as stat_module
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Request
from pydantic import BaseModel

from traffic_analyzer.web.workspace.core import (
    PRUNED_DIR_NAMES,
    VIDEO_EXTENSIONS,
    _TTLCache,
    _pkg_var,
    analysis_dir,
    has_results,
    invalidate_caches,
    register_cache,
    require_workspace,
    router,
    validate_stem,
)


def list_videos(workspace: Path) -> List[Dict[str, Any]]:
    """All videos in the workspace at any depth (dot-dirs and PRUNED_DIR_NAMES skipped).

    Each entry carries the workspace-relative path (``rel``) used by the
    frontend as its unique key; ``has_results`` follows the flat
    ``analysis/<stem>/`` contract for every depth.
    """
    videos: List[Dict[str, Any]] = []
    root = workspace.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        # 点目录与产出目录(PRUNED_DIR_NAMES)原地剔除,os.walk 不再下钻。
        dirnames[:] = [
            d for d in dirnames if not d.startswith(".") and d not in PRUNED_DIR_NAMES
        ]
        for name in filenames:
            # 纯字符串的扩展名判断先行(os.path.splitext 与 Path.suffix 规则一致),
            # 非视频文件不再付出 Path 构造/lstat 的代价。
            if os.path.splitext(name)[1].lower() not in VIDEO_EXTENSIONS:
                continue
            path = Path(dirpath) / name
            try:
                lst = path.lstat()
            except OSError:
                continue
            if stat_module.S_ISLNK(lst.st_mode):
                # 符号链接可能指到工作区外,保留原有的 resolve 逃逸检查;
                # size/mtime 取链接目标(与原 stat 行为一致)。
                resolved = path.resolve()
                if resolved != root and root not in resolved.parents:
                    continue  # symlink escaping the workspace
                try:
                    st = path.stat()
                except OSError:
                    continue
            else:
                # 非链接文件 lstat 与 stat 等价:合并为一次系统调用,
                # 且 os.walk(followlinks=False) 不下钻链接目录、root 已 resolve,
                # 路径不可能逃逸工作区,无需逐文件 resolve。
                st = lst
            videos.append(
                {
                    "name": name,
                    "stem": path.stem,
                    "rel": path.relative_to(root).as_posix(),
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "has_results": has_results(workspace, path.stem),
                }
            )
    videos.sort(key=lambda v: v["rel"])
    return videos


# 视频列表/看板这类昂贵聚合的缓存改为「长 TTL + 主动失效」主导:
# 原 15s 短 TTL 的自然过期让大工作区(外接盘,冷建秒级)在翻页/切换时频繁
# 踩冷建;正确性本就由 invalidate_caches 保证(workspace 变更 / infer 完成 /
# review PUT / SFT/证据 PUT 全量失效),TTL 只作为进程外直接改盘的兜底刷新。
_VIDEOS_CACHE_TTL_SEC = 120.0


class _LongTTLCache(_TTLCache):
    """实例级 TTL 的 _TTLCache:过期判断用自身 ttl,不跟随全局 _CACHE_TTL_SEC。

    语义(命中/失效/深拷贝契约)与父类一致,仅 TTL 来源不同;测试可调短
    实例的 ``_ttl_sec`` 验证自然过期路径。
    """

    def __init__(self, ttl_sec: float) -> None:
        super().__init__()
        self._ttl_sec = ttl_sec

    def get(self, key: str, copy: bool = True) -> Optional[Any]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            ts, value = entry
            if time.monotonic() - ts > self._ttl_sec:
                del self._entries[key]
                return None
        if copy:
            return copy_module.deepcopy(value)
        return value


_videos_cache = register_cache(_LongTTLCache(_VIDEOS_CACHE_TTL_SEC))


def list_videos_cached(workspace: Path) -> List[Dict[str, Any]]:
    """list_videos 的进程内缓存版(key=workspace 路径,TTL 见 _VIDEOS_CACHE_TTL_SEC)。

    /api/workspace/videos 与 dashboard 聚合共用;失效统一走
    invalidate_caches(workspace 变更 / infer 完成 / review / SFT / 证据 PUT)。
    copy=False:返回值只读使用(端点直接序列化,不就地修改)。
    """
    key = str(workspace)
    cached = _videos_cache.get(key, copy=False)
    if cached is not None:
        return cached
    # 经包命名空间调用:测试 monkeypatch workspace.list_videos(spy 计数)须生效。
    videos = _pkg_var("list_videos", list_videos)(workspace)
    _videos_cache.set(key, videos)
    return videos


@router.get("/api/workspace/videos")
def get_workspace_videos(request: Request) -> List[Dict[str, Any]]:
    return list_videos_cached(require_workspace(request))


# ---------------------------------------------------------------------------
# 分析报告删除:analysis/<stem>/ 整目录(报告/SFT/证据/图片一并移除)。
# 「已完成」徽标由 has_results 按 analysis/<stem>/ 存在性推导(core.py),
# 目录删除后状态自然消失;videos/dashboard 的进程内缓存统一走
# invalidate_caches 失效。目录不存在时幂等(existed=false,仍返回 ok)。
# ---------------------------------------------------------------------------


class AnalysisDeleteRequest(BaseModel):
    stems: List[str]


def _remove_analysis_dir(workspace: Path, stem: str) -> Dict[str, Any]:
    """删 analysis/<stem>/ 整目录并失效缓存;返回 {ok, existed}。

    幂等:目录不存在直接 ok(existed=False);rmtree 失败(ok=False)时
    existed 保持 True——盘上仍在。
    """
    adir = analysis_dir(workspace, stem)
    if not adir.is_dir():
        return {"ok": True, "existed": False}
    try:
        shutil.rmtree(adir)
    except OSError as exc:
        return {"ok": False, "existed": True, "error": str(exc)}
    invalidate_caches()  # 徽标按目录存在性推导:删后立即重算(videos/dashboard 共用)
    return {"ok": True, "existed": True}


@router.delete("/api/workspace/analysis/{stem}")
def delete_analysis(stem: str, request: Request) -> Dict[str, Any]:
    workspace = require_workspace(request)
    validate_stem(stem)  # 越界 stem(..、路径分隔符等)→ 404
    result = _remove_analysis_dir(workspace, stem)
    return {"stem": stem, **result}


@router.post("/api/workspace/analysis/delete")
def delete_analysis_batch(
    body: AnalysisDeleteRequest, request: Request
) -> List[Dict[str, Any]]:
    """批量删报告:逐项独立处理,逐项回 {stem, ok, existed[, error]}。

    非法 stem 不中断批次(该项 ok=False),其余照常删除。
    """
    workspace = require_workspace(request)
    results: List[Dict[str, Any]] = []
    for stem in body.stems:
        try:
            validate_stem(stem)
        except HTTPException:
            results.append({"stem": stem, "ok": False, "existed": False})
            continue
        results.append({"stem": stem, **_remove_analysis_dir(workspace, stem)})
    return results
