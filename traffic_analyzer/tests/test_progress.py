"""Tests for the per-expert progress reporter (utils/progress.py)."""

from __future__ import annotations

import json
import threading

import pytest

from traffic_analyzer.utils import progress as progress_mod


@pytest.fixture()
def reporter():
    """Yield a fresh-state singleton reporter (tests run non-TTY -> marker lines)."""
    rep = progress_mod.get_reporter()
    yield rep
    with rep._lock:
        rep._shutdown_live_locked()
        rep._reset_state()


class TestMarkerLines:
    def test_register_start_phase_done_sequence(self, reporter, capsys):
        reporter.register(["违法停车", "交通事故", "裁决"])
        reporter.start("违法停车")
        reporter.phase("违法停车", "prepare")
        reporter.phase("违法停车", "main_detect")
        reporter.done("违法停车", True)
        reporter.start("交通事故")
        reporter.error("交通事故")
        reporter.phase("裁决", "prepare")
        reporter.done("裁决", None)

        lines = capsys.readouterr().out.strip().splitlines()
        assert lines[0] == "EXPERT_PROGRESS|register|3|违法停车,交通事故,裁决"
        assert "EXPERT_PROGRESS|start|违法停车" in lines
        assert "EXPERT_PROGRESS|phase|违法停车|0.05|扫描路肩与车道边缘" not in lines
        assert "EXPERT_PROGRESS|phase|违法停车|0.05|选帧备料" in lines
        assert "EXPERT_PROGRESS|phase|违法停车|0.15|扫描路肩与车道边缘" in lines
        assert "EXPERT_PROGRESS|done|1/3|违法停车|detected" in lines
        assert "EXPERT_PROGRESS|done|2/3|交通事故|error" in lines
        assert "EXPERT_PROGRESS|phase|裁决|0.30|汇总候选" in lines
        assert "EXPERT_PROGRESS|done|3/3|裁决|done" in lines

    def test_fraction_two_decimals(self, reporter, capsys):
        reporter.register(["道路施工"])
        reporter.start("道路施工")
        reporter.phase("道路施工", "evidence")
        out = capsys.readouterr().out
        assert "EXPERT_PROGRESS|phase|道路施工|0.30|合成施工证据画廊" in out

    def test_done_token_undetected(self, reporter, capsys):
        reporter.register(["摩托车出现"])
        reporter.start("摩托车出现")
        reporter.done("摩托车出现", False)
        out = capsys.readouterr().out
        assert "EXPERT_PROGRESS|done|1/1|摩托车出现|undetected" in out


class TestThreadLocalPhase:
    def test_phase_without_name_uses_thread_local(self, reporter, capsys):
        reporter.register(["应急车道占用", "裁决"])
        reporter.start("应急车道占用")  # sets thread-local on this thread
        reporter.phase("prepare")
        reporter.phase("evidence")
        out = capsys.readouterr().out
        assert "EXPERT_PROGRESS|phase|应急车道占用|0.05|标定应急车道区域" in out
        assert "EXPERT_PROGRESS|phase|应急车道占用|0.30|标注占道车辆与压线" in out

    def test_phase_without_name_on_fresh_thread_is_noop(self, reporter, capsys):
        reporter.register(["拥堵", "裁决"])

        def _worker():
            reporter.phase("prepare")  # no thread-local name -> no output

        t = threading.Thread(target=_worker)
        t.start()
        t.join()
        out = capsys.readouterr().out
        assert "EXPERT_PROGRESS|phase" not in out


class TestFallbacks:
    def test_missing_json_falls_back_to_builtin(self, reporter, capsys, monkeypatch, tmp_path):
        monkeypatch.setattr(progress_mod, "_PHASES_PATH", tmp_path / "nonexistent.json")
        reporter._phases = None
        reporter.register(["交通事故"])
        reporter.start("交通事故")
        reporter.phase("交通事故", "main_detect")
        reporter.done("交通事故", True)
        out = capsys.readouterr().out
        assert "EXPERT_PROGRESS|phase|交通事故|0.15|主检测分析" in out

    def test_corrupt_json_falls_back_to_builtin(self, reporter, capsys, monkeypatch, tmp_path):
        bad = tmp_path / "expert_phases.json"
        bad.write_text("{ not json !!!", encoding="utf-8")
        monkeypatch.setattr(progress_mod, "_PHASES_PATH", bad)
        reporter._phases = None
        reporter.register(["裁决"])
        reporter.phase("裁决", "main_detect")
        out = capsys.readouterr().out
        assert "EXPERT_PROGRESS|phase|裁决|0.60|交叉裁决" in out

    def test_unknown_category_falls_back_to_default_labels(self, reporter, capsys):
        reporter.register(["抛洒物"])
        reporter.start("抛洒物")
        reporter.phase("抛洒物", "evidence")
        out = capsys.readouterr().out
        assert "EXPERT_PROGRESS|phase|抛洒物|0.30|证据合成" in out

    def test_missing_phase_key_falls_back_to_default(self, reporter, capsys):
        reporter.register(["违法停车"])
        reporter.start("违法停车")
        reporter.phase("违法停车", "finish")  # not customized -> default label
        out = capsys.readouterr().out
        assert "EXPERT_PROGRESS|phase|违法停车|0.90|收尾" in out


class TestRobustness:
    def test_unknown_lane_is_ignored(self, reporter, capsys):
        reporter.register(["违法停车"])
        reporter.start("不存在的类别")
        reporter.phase("不存在的类别", "prepare")
        reporter.done("不存在的类别", True)
        out = capsys.readouterr().out
        assert "不存在的类别" not in out

    def test_calls_before_register_do_not_raise(self):
        rep = progress_mod.ProgressReporter()
        rep.start("x")
        rep.phase("x", "prepare")
        rep.done("x", True)
        rep.error("x")

    def test_double_done_ignored(self, reporter, capsys):
        reporter.register(["道路施工"])
        reporter.start("道路施工")
        reporter.done("道路施工", True)
        reporter.done("道路施工", False)
        out = capsys.readouterr().out
        assert out.count("EXPERT_PROGRESS|done") == 1
        assert "EXPERT_PROGRESS|done|1/1|道路施工|detected" in out

    def test_phase_fraction_never_regresses(self, reporter, capsys):
        reporter.register(["车辆逆行/倒车"])
        reporter.start("车辆逆行/倒车")
        reporter.phase("车辆逆行/倒车", "reflect")
        reporter.phase("车辆逆行/倒车", "prepare")
        lane = reporter._lanes["车辆逆行/倒车"]
        assert lane.target == pytest.approx(0.70)

    def test_concurrent_experts(self, reporter, capsys):
        names = ["违法停车", "应急车道占用", "交通事故", "高速公路行人出现"]
        reporter.register(names)

        def _run(name):
            reporter.start(name)
            for key in ("prepare", "main_detect", "parse", "reflect", "finish"):
                reporter.phase(key)
            reporter.done(name, True)

        threads = [threading.Thread(target=_run, args=(n,)) for n in names]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        out = capsys.readouterr().out
        for i, name in enumerate(names, start=1):
            assert f"EXPERT_PROGRESS|done|{i}/4|" in out
        assert out.count("EXPERT_PROGRESS|start|") == 4


class TestProgressFileSink:
    """结构化 sink:TRAFFIC_ANALYZER_PROGRESS_FILE 非空时写 JSONL,stdout 标记不变。"""

    def test_events_appended_as_jsonl(self, reporter, monkeypatch, tmp_path, capsys):
        path = tmp_path / "progress.jsonl"
        monkeypatch.setenv(progress_mod.PROGRESS_FILE_ENV, str(path))
        reporter.register(["违法停车", "裁决"])
        reporter.start("违法停车")
        reporter.phase("违法停车", "prepare")
        reporter.done("违法停车", True)
        reporter.done("裁决", None)

        events = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
        ]
        assert [e["type"] for e in events] == [
            "register", "start", "phase",
            "phase", "lane_done",  # 违法停车 收尾:phase 1.0 + lane_done
            "phase", "lane_done",  # 裁决
        ]
        assert events[0]["total"] == 2
        assert events[0]["lanes"] == ["违法停车", "裁决"]
        assert events[1]["lane"] == "违法停车"
        assert events[2]["lane"] == "违法停车"
        assert events[2]["fraction"] == pytest.approx(0.05)
        assert events[2]["label"] == "选帧备料"
        assert events[4]["result"] == "detected"
        assert events[4]["done"] == 1 and events[4]["total"] == 2
        assert events[6]["result"] == "done"  # 裁决无检出语义
        assert all(isinstance(e["ts"], float) for e in events)
        # stdout 标记行保持不变(CLI 人类可读)。
        out = capsys.readouterr().out
        assert "EXPERT_PROGRESS|register|2|违法停车,裁决" in out
        assert "EXPERT_PROGRESS|done|1/2|违法停车|detected" in out

    def test_env_unset_is_noop(self, reporter, monkeypatch, tmp_path):
        monkeypatch.delenv(progress_mod.PROGRESS_FILE_ENV, raising=False)
        reporter.register(["裁决"])
        reporter.start("裁决")
        reporter.done("裁决", None)
        assert list(tmp_path.iterdir()) == []  # 未创建任何文件

    def test_emit_step_and_run_done(self, monkeypatch, tmp_path):
        path = tmp_path / "progress.jsonl"
        monkeypatch.setenv(progress_mod.PROGRESS_FILE_ENV, str(path))
        progress_mod.emit_step(1, 4, "预处理")
        progress_mod.emit_step(3.5, 4, "SFT")
        progress_mod.emit_run_done()
        events = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
        ]
        assert events[0]["type"] == "step"
        assert events[0]["step"] == 1 and events[0]["total"] == 4
        assert events[0]["name"] == "预处理"
        assert events[1]["step"] == 3.5
        assert events[2] == {
            "type": "done", "status": "ok", "ts": events[2]["ts"]
        }

    def test_emit_step_env_unset_noop(self, monkeypatch, tmp_path):
        monkeypatch.delenv(progress_mod.PROGRESS_FILE_ENV, raising=False)
        progress_mod.emit_step(1, 4, "预处理")  # must not raise
        progress_mod.emit_run_done()
        assert list(tmp_path.iterdir()) == []

    def test_unwritable_path_never_raises(self, reporter, monkeypatch, tmp_path):
        monkeypatch.setenv(
            progress_mod.PROGRESS_FILE_ENV, str(tmp_path / "nope" / "p.jsonl")
        )
        reporter.register(["裁决"])  # 目录不存在:吞掉,绝不影响推理
        reporter.done("裁决", None)
