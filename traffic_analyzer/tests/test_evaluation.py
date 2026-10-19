"""evaluation.event_names 的 YAML 派生与缓存失效测试。

D 任务验收:事件名从 event_categories.yaml 派生(不再有字面量清单),
fixture yaml 改名后索引跟随变化(仿 test_evidence_sft 的
TestYamlCacheInvalidation 的 mtime 失效口径)。
"""

from __future__ import annotations

import time
from pathlib import Path

import yaml

from traffic_analyzer.evaluation import (
    NORMAL_EVENT_ID,
    event_names,
    extract_gt_from_filename,
)


def _write_categories(path: Path, name_zh: str) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "event_categories": [
                    {"event_id": 1, "name_zh": name_zh, "is_active": True},
                    {"event_id": 2, "name_zh": "应急车道占用", "is_active": True},
                ]
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )


class TestEventNamesDerivedFromYaml:
    def test_follows_yaml_rename(self, tmp_path: Path) -> None:
        """fixture yaml 事件改名后,下一次读取跟随(mtime 失效,无需重启)。"""
        cfg = tmp_path / "event_categories.yaml"
        _write_categories(cfg, "违法停车")

        first = event_names(cfg)
        assert first[1] == "违法停车"
        assert NORMAL_EVENT_ID in first  # 9 = 正常占位始终存在
        assert first[NORMAL_EVENT_ID] == "无异常事件"

        # mtime 需前进才会失效(cache key 含 mtime_ns)
        cfg.write_text("", encoding="utf-8")
        _write_categories(cfg, "临时改名的事件")
        fut = time.time() + 10
        import os

        os.utime(cfg, (fut, fut))

        second = event_names(cfg)
        assert second[1] == "临时改名的事件"

    def test_extract_gt_accepts_yaml_defined_ids(self, tmp_path: Path) -> None:
        """文件名 GT 提取按派生索引判定合法编号:fixture 定义 1/2 与 9 占位。"""
        cfg = tmp_path / "event_categories.yaml"
        _write_categories(cfg, "违法停车")
        names = event_names(cfg)

        assert extract_gt_from_filename("01-02-09_Event_x.mp4", names) == {1, 2, 9}
        # fixture 未定义的编号(如 11)被静默跳过
        assert extract_gt_from_filename("01-11_Event_x.mp4", names) == {1}

    def test_default_source_matches_repo_yaml(self) -> None:
        """缺省路径读仓库真实 event_categories.yaml:活跃事件全覆盖 + 9 占位。"""
        names = event_names()
        assert NORMAL_EVENT_ID in names
        assert {1, 2, 3, 4, 5, 6, 7, 8, 10, 11} <= set(names)
