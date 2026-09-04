"""Unit tests for rag/build.py(build_index 编排:过滤 / 进度回调 / 取消 / 失败清单).

[文件说明]
作用:测试 build_index 的 only_missing / refresh_annotations 过滤、limit、
progress_cb(done, total, failed) 回调、cancel_flag 条间取消(partial + 已完成保留)、
单视频失败记清单继续;embed_video_bytes / embed_texts / extract_site 全部
monkeypatch mock,tmp_path 假视频 + 假标注,无网络。
上游:pytest 自动发现并执行本文件测试。
下游:traffic_analyzer/rag/build.py(被测模块)、rag/store.py(断言落库)。
"""

from __future__ import annotations

import json

import pytest

from traffic_analyzer.rag import build as build_mod
from traffic_analyzer.rag.build import build_index
from traffic_analyzer.rag.store import RagStore

STEM_A = "01-02_Event_129_1751869790726_1"
STEM_B = "02-08_Event_257_1754288341555_1"
STEM_C = "01-08_Event_129_1756001969701_1"

_SITE = {"road": "G3京台高速", "stake": "K18+470", "direction": "进京", "camera": "3"}


def _make_workspace(workspace, stems) -> None:
    for stem in stems:
        (workspace / f"{stem}.mp4").write_bytes(stem.encode())


def _write_label(workspace, stem, text="应急车道停车", events=(2,)) -> None:
    out_dir = workspace / "analysis" / stem
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{stem}.json").write_text(
        json.dumps(
            {
                "action": list(events),
                "description": f"<think>\n{text}\n</think>\n<answer>\nclass2\n</answer>",
                "start_timestamp": 0.0,
                "end_timestamp": 6.0,
                "last_edited_by": "human",
                "last_edited_at": "2026-08-01T00:00:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def mock_services(monkeypatch: pytest.MonkeyPatch) -> dict:
    """mock embedding / OSD;calls 记录 embed_video_bytes 收到的字节(= stem)。"""
    calls: list[str] = []

    def _embed_video(data: bytes, ext: str = "mp4") -> list[float]:
        calls.append(data.decode())
        if "FAIL" in data.decode():
            raise RuntimeError("embed boom")
        return [1.0, 0.0]

    monkeypatch.setattr(build_mod, "embed_video_bytes", _embed_video)
    monkeypatch.setattr(build_mod, "embed_texts", lambda texts: [[0.0, 1.0] for _ in texts])
    monkeypatch.setattr(build_mod, "extract_site", lambda video, cache: dict(_SITE))
    return {"video_calls": calls}


class TestBuildIndex:
    def test_basic_build(self, tmp_path, mock_services) -> None:
        _make_workspace(tmp_path, [STEM_A, STEM_B])
        _write_label(tmp_path, STEM_A)
        progress: list[tuple[int, int, int]] = []
        result = build_index(
            tmp_path,
            concurrency=2,
            progress_cb=lambda d, t, f: progress.append((d, t, f)),
        )
        assert result["partial"] is False
        assert result["total"] == 2
        assert sorted(result["success"]) == [f"{STEM_A}.mp4", f"{STEM_B}.mp4"]
        assert result["failed"] == []
        assert progress[-1] == (2, 2, 0)
        assert len(progress) == 2
        stats = result["stats"]
        assert stats["total"] == 2
        assert stats["has_annotation"] == 1
        assert stats["meta"]["built_at"]
        with RagStore(tmp_path) as store:
            rows = {r["video_path"]: r for r in store.records()}
        a = rows[f"{STEM_A}.mp4"]
        assert a["events"] == [2]
        assert a["has_annotation"] == 1
        assert a["human_edited"] == 1
        assert a["site"] == "G3京台高速-K18+470-进京-3"
        assert a["start_ts"] == pytest.approx(1751869790.726)
        assert a["duration_s"] == 6.0
        assert rows[f"{STEM_B}.mp4"]["has_annotation"] == 0

    def test_only_missing_skips_existing(self, tmp_path, mock_services) -> None:
        _make_workspace(tmp_path, [STEM_A, STEM_B])
        build_index(tmp_path, concurrency=2)
        mock_services["video_calls"].clear()
        result = build_index(tmp_path, concurrency=2)
        assert result["total"] == 0
        assert result["success"] == []
        assert mock_services["video_calls"] == []
        # 新视频加入后只处理增量
        _make_workspace(tmp_path, [STEM_C])
        result = build_index(tmp_path, concurrency=2)
        assert result["total"] == 1
        assert mock_services["video_calls"] == [STEM_C]

    def test_refresh_annotations_reprocesses_labeled(self, tmp_path, mock_services) -> None:
        _make_workspace(tmp_path, [STEM_A, STEM_B])
        _write_label(tmp_path, STEM_A)
        build_index(tmp_path, concurrency=2)
        mock_services["video_calls"].clear()
        # 标注未变更:refresh_annotations 也不重算(时间戳与建库时一致)
        result = build_index(tmp_path, concurrency=2, refresh_annotations=True)
        assert result["total"] == 0
        assert mock_services["video_calls"] == []
        # 标注被编辑(last_edited_at 变化)→ 仅该视频重算
        _write_label(tmp_path, STEM_A, text="应急车道停车(改)")
        out = tmp_path / "analysis" / STEM_A / f"{STEM_A}.json"
        data = json.loads(out.read_text(encoding="utf-8"))
        data["last_edited_at"] = "2026-08-02T00:00:00"
        out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        result = build_index(tmp_path, concurrency=2, refresh_annotations=True)
        assert result["total"] == 1
        assert mock_services["video_calls"] == [STEM_A]
        # only_missing=False 时全量重建
        mock_services["video_calls"].clear()
        result = build_index(
            tmp_path, concurrency=2, only_missing=False, refresh_annotations=True
        )
        assert result["total"] == 2
        assert sorted(mock_services["video_calls"]) == [STEM_A, STEM_B]

    def test_limit(self, tmp_path, mock_services) -> None:
        _make_workspace(tmp_path, [STEM_A, STEM_B, STEM_C])
        result = build_index(tmp_path, concurrency=2, limit=2)
        assert result["total"] == 2
        assert len(result["success"]) == 2
        assert result["stats"]["total"] == 2

    def test_failure_recorded_and_continues(self, tmp_path, mock_services) -> None:
        _make_workspace(tmp_path, [STEM_A, "FAIL_1751869790726"])
        progress: list[tuple[int, int, int]] = []
        result = build_index(
            tmp_path,
            concurrency=2,
            progress_cb=lambda d, t, f: progress.append((d, t, f)),
        )
        assert result["success"] == [f"{STEM_A}.mp4"]
        assert len(result["failed"]) == 1
        assert result["failed"][0]["video"] == "FAIL_1751869790726.mp4"
        assert "RuntimeError" in result["failed"][0]["error"]
        assert progress[-1] == (2, 2, 1)
        assert result["stats"]["total"] == 1

    def test_cancel_flag_stops_partial(self, tmp_path, mock_services) -> None:
        _make_workspace(tmp_path, [STEM_A, STEM_B, STEM_C])
        cancelled = {"flag": False}

        def _progress(done, total, failed) -> None:
            cancelled["flag"] = True  # 第一条落库后即请求取消

        result = build_index(
            tmp_path,
            concurrency=1,
            progress_cb=_progress,
            cancel_flag=lambda: cancelled["flag"],
        )
        assert result["partial"] is True
        assert len(result["success"]) == 1  # 已完成的保留
        assert result["stats"]["total"] == 1

    def test_no_cancel_runs_all(self, tmp_path, mock_services) -> None:
        _make_workspace(tmp_path, [STEM_A, STEM_B, STEM_C])
        result = build_index(tmp_path, concurrency=2, cancel_flag=lambda: False)
        assert result["partial"] is False
        assert len(result["success"]) == 3
