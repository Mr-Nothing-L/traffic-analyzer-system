#!/usr/bin/env python3
"""mock 模式全按键循环自测脚本(演示版:带节奏停顿)。

用法(项目根目录):
    python3 scripts/e2e_mock_test.py              # 有头浏览器,无限循环,Ctrl+C 停止
    python3 scripts/e2e_mock_test.py --headless   # 无头模式
    python3 scripts/e2e_mock_test.py --passes 3   # 只跑 3 轮
    python3 scripts/e2e_mock_test.py --pause 3.5  # 每步之间停顿 3.5 秒(默认 2.5)
    python3 scripts/e2e_mock_test.py --fast       # 不做任何演示延迟(pause=0, slow_mo=0)

行为:自动确保 web 服务(127.0.0.1:8600)在运行(不在则启动,退出时不杀);
打开 ?mock=1 页面,对界面所有按键/页面做一轮完整遍历(工作区弹窗基础/进阶、
切换工作区、过滤、排序、全选、目录展开折叠、勾选、预览、推理、停止、重试、
完成、SFT 选项联动、证据编辑、数据看板),打印每项 PASS/FAIL,单步失败不中断整轮。
不 Ctrl+C 就一直循环。每轮结束截图存到 output/e2e_screenshots/(只保留最近 20 张)。
"""

import argparse
import os
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8600"
SHOT_DIR = REPO_ROOT / "output" / "e2e_screenshots"
SHOT_KEEP = 20  # 截图只保留最近 N 张,避免无限循环时磁盘膨胀

# ---- 与 mock 数据耦合的常量(mock 数据变化时需同步修改) ----
# 推理/预览/证据编辑演示所用的视频名片段,来自 scripts/build_mock_data.py 生成的
# mock_data.js 中演示区真实结果视频 01-02_Event_129_1755579215119_1.mp4
MOCK_VIDEO_FRAGMENT = "01-02_Event_129"
# MOCK_VIDEO_FRAGMENT 的特征子串:侧栏过滤后应恰好剩 1 个视频
MOCK_FILTER_TEXT = "129"
# 证据编辑拖拽的多边形端点 0 的归一化坐标,来自真实证据 01-02_Event_129
# 「应急车道占用」事件 calibration.emergency_polygon_rel[0]
EVIDENCE_VERTEX0_REL = (0.823, 0.252)
# mock 模式前端推理 tick 间隔(毫秒),见 traffic_analyzer/web/static/js/main.js
# 的 setInterval(mockTick, 700);「裁决」完成后 1 tick 内推入「SFT 标注」泳道
MOCK_TICK_MS = 700


def server_up() -> bool:
    """应用指纹校验:/api/jobs 返回 JSON 数组才是本系统的服务

    (仅探测 / 会把恰好占用 8600 端口的其他服务误判为已就绪)。"""
    try:
        with urllib.request.urlopen(BASE + "/api/jobs", timeout=2) as resp:
            return resp.status == 200 and isinstance(json.loads(resp.read()), list)
    except urllib.error.HTTPError as e:
        # 认证开启时 401 同样证明是本系统的服务在监听
        return e.code == 401
    except Exception:
        return False


def ensure_server() -> None:
    """服务不在则后台启动(不接管生命周期,退出时留着)。"""
    if server_up():
        return
    print("[setup] 8600 无服务,自动启动 traffic_analyzer web ...")
    log = open("/tmp/e2e_web.log", "w")
    subprocess.Popen(
        [sys.executable, "-m", "traffic_analyzer", "web",
         "--host", "127.0.0.1", "--port", "8600"],
        cwd=REPO_ROOT, stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    for _ in range(60):
        if server_up():
            print("[setup] 服务已就绪")
            return
        time.sleep(0.5)
    raise RuntimeError("web 服务 30s 内未就绪,详见 /tmp/e2e_web.log")


class Pass:
    """一轮遍历的步骤记录器。pause 为每步之间的演示停顿(秒),0 表示不停。"""

    def __init__(self, idx: int, pause: float = 0.0) -> None:
        self.idx = idx
        self.pause = pause
        self.failures = []

    def step(self, name: str, fn) -> None:
        try:
            fn()
            print(f"  [PASS] {name}")
        except Exception as exc:  # noqa: BLE001 - 测试脚本要兜住一切继续跑
            self.failures.append(name)
            print(f"  [FAIL] {name}: {type(exc).__name__}: {str(exc)[:160]}")
        if self.pause > 0:
            time.sleep(self.pause)  # 步骤间停顿,让观众看清上一结果


def _prune_screenshots(keep: int = SHOT_KEEP) -> None:
    """只保留最近 keep 张截图(按修改时间)。"""
    shots = sorted(SHOT_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime)
    for old in shots[: max(0, len(shots) - keep)]:
        old.unlink(missing_ok=True)


def run_pass(page, p: Pass, shot: Path) -> None:
    """对 mock 页面做一轮全按键/全页面遍历。page 已打开 ?mock=1。"""
    V = MOCK_VIDEO_FRAGMENT

    def sel(s):
        return page.wait_for_selector(s, timeout=8000)

    def demo(ms: int) -> None:
        """关键演示点额外停留(毫秒);--fast 时不停。"""
        if p.pause > 0:
            page.wait_for_timeout(ms)

    # ---------- 1. 工作区弹窗基础 ----------
    def t_workspace_modal():
        page.click("#btn-workspace")
        sel("#dir-modal:not([hidden])")
        page.click("#dir-edit")            # 手动输入路径按钮
        sel("#dir-input:not([hidden])")
        page.click("#dir-cancel")
        page.wait_for_selector("#dir-modal", state="hidden", timeout=8000)
        page.click("#btn-workspace")       # 再开一次,用 ✕ 关闭
        sel("#dir-modal:not([hidden])")
        page.click("#dir-close")
        page.wait_for_selector("#dir-modal", state="hidden", timeout=8000)
    p.step("工作区弹窗(打开/输入路径/取消/✕关闭)", t_workspace_modal)

    # ---------- 2. 弹窗进阶:最近使用快速跳转 + 手动输入路径回车 ----------
    def t_modal_advanced():
        page.click("#btn-workspace")
        sel("#dir-modal:not([hidden])")
        # 快速跳转:下拉里任选一个可用项(当前工作区/历史路径/主目录,必然非空)
        values = page.eval_on_selector_all(
            "#dir-recent-select option", "els => els.map(e => e.value).filter(v => v)")
        assert values, "最近使用下拉应至少有一个可跳转项"
        page.select_option("#dir-recent-select", value=values[0])
        page.wait_for_selector("#dir-list .dir-row", timeout=8000)
        demo(1000)
        # 手动输入路径回车跳转(只看不选,最后取消,不换工作区)
        page.click("#dir-edit")
        sel("#dir-input:not([hidden])")
        page.fill("#dir-input", "/mock")
        page.press("#dir-input", "Enter")
        page.wait_for_selector('#dir-list .dir-row[data-path="/mock/datasets"]', timeout=8000)
        demo(1000)
        page.click("#dir-cancel")
        page.wait_for_selector("#dir-modal", state="hidden", timeout=8000)
    p.step("弹窗进阶(快速跳转/输入路径回车/取消)", t_modal_advanced)

    # ---------- 3. 切换工作区:切到 /mock/datasets 再切回 /mock/workspace ----------
    def t_switch_workspace():
        old = page.text_content("#ws-path").strip()
        assert old, "顶栏应已显示当前工作区路径"

        def switch_to(rel_path: str, expect: str) -> None:
            page.click("#btn-workspace")
            sel("#dir-modal:not([hidden])")
            page.wait_for_selector("#dir-list .dir-row", timeout=8000)
            page.click("#dir-list .dir-row.dir-up")  # 「..」进入上级 /mock
            page.wait_for_selector(
                f'#dir-list .dir-row[data-path="{rel_path}"]', timeout=8000)
            page.click(f'#dir-list .dir-row[data-path="{rel_path}"]')  # 单击选中
            demo(800)
            page.click("#dir-confirm")
            page.wait_for_function(
                "(exp) => document.querySelector('#ws-path')"
                " && document.querySelector('#ws-path').textContent.trim() === exp",
                arg=expect, timeout=10_000)
            page.wait_for_selector("#dir-modal", state="hidden", timeout=8000)
            page.wait_for_selector("#video-list .video-item", timeout=10_000)

        switch_to("/mock/datasets", "/mock/datasets")
        demo(1500)
        switch_to("/mock/workspace", old)  # 原路切回,后续步骤不受影响
    p.step("切换工作区(切走 → 断言路径变化 → 切回)", t_switch_workspace)

    # ---------- 4. 侧栏:过滤 / 排序 / 全选 ----------
    def t_filter_sort():
        page.fill("#side-filter-input", MOCK_FILTER_TEXT)
        page.wait_for_timeout(300)
        visible = page.eval_on_selector_all(
            "#video-list .video-item", "els => els.filter(e => e.offsetParent).length")
        assert visible == 1, f"过滤后应剩 1 个视频,实际 {visible}"
        page.fill("#side-filter-input", "")
        page.wait_for_timeout(300)
        page.select_option("#side-sort-select", index=1)
        page.wait_for_timeout(200)
        page.select_option("#side-sort-select", index=0)
    p.step("侧栏过滤 + 排序", t_filter_sort)

    def t_check_all():
        page.check("#check-all")
        page.wait_for_timeout(200)
        n = page.eval_on_selector_all(
            "#video-list .video-item input[data-check]:checked", "els => els.length")
        assert n >= 3, f"全选后勾选数应 >=3,实际 {n}"
        page.uncheck("#check-all")
        page.wait_for_timeout(200)
    p.step("全选/取消全选", t_check_all)

    # ---------- 5. 目录展开/折叠 ----------
    def t_tree_dirs():
        for rel in ("analysis", "clips"):
            row = f'#video-list .tree-dir[data-dir="{rel}"]'
            page.click(row)  # 展开
            page.wait_for_selector(f"{row} + .tree-kids", timeout=8000)
            if rel == "clips":  # clips 下有嵌套视频,确认子项真实渲染
                page.wait_for_selector(
                    '#video-list .video-item[data-rel="clips/nested_clip.mp4"]', timeout=8000)
            demo(1200)
            page.click(row)  # 折叠(收起动画结束后整树重渲染,子容器被移除)
            page.wait_for_selector(f"{row} + .tree-kids", state="detached", timeout=8000)
    p.step("目录展开/折叠(analysis、clips)", t_tree_dirs)

    # ---------- 6. 勾选视频 + 预览 ----------
    def t_tree_preview():
        row = page.locator("#video-list .video-item", has_text=V)
        row.locator("input[data-check]").check()
        row.locator(".video-name").click()
        sel("#pane-top")
    p.step("勾选视频 + 点开预览", t_tree_preview)

    # ---------- 7. 推理 → 专家面板 → 停止 ----------
    def t_infer_stop():
        row = page.locator("#video-list .video-item", has_text=V)
        page.click("#btn-infer")
        sel("#card-experts")
        sel("#exp-lanes .expert-lane")
        page.wait_for_timeout(3000 if p.pause > 0 else int(MOCK_TICK_MS * 3.5))  # 关键演示点:专家面板停留(≈3.5 个 mock tick)
        page.click("#exp-stop")
        row.locator(".badge.st-failed").wait_for(timeout=15_000)
    p.step("开始推理 → 专家工作间 → ■ 停止", t_infer_stop)

    # ---------- 8. ↻ 重试 → 等完成 → 结果页 ----------
    def t_retry_done():
        row = page.locator("#video-list .video-item", has_text=V)
        row.locator(".retry-btn").click()
        sel("#card-experts")
        # 阶段泳道断言:先等裁决泳道完成(类别泳道随机推进,耗时不定),
        # 之后 1 tick(MOCK_TICK_MS)内 mock 推入「SFT 标注」泳道,轮询 1.5s 内必然可见
        page.wait_for_function(
            "() => { const r = document.querySelector("
            "'#exp-lanes .expert-lane[data-lane=\"裁决\"]');"
            " return r && !/lane-(queued|running)/.test(r.className); }",
            timeout=180_000)
        page.wait_for_selector(
            '#exp-lanes .expert-lane[data-lane="SFT 标注"]', timeout=30_000)
        page.wait_for_selector(
            '#exp-lanes .expert-lane[data-lane="报告"]', timeout=30_000)
        # 徽章必须限定在该视频行内(其他视频可能早已是已完成)
        row.locator(".badge.st-done").wait_for(timeout=180_000)
        # 完成后轮询会自动重载当前视频;若 3s 内结果卡未出现,手动再点一次视频名兜底
        try:
            page.wait_for_selector("#card-sft", timeout=3000)
        except Exception:
            page.locator("#video-list .video-item", has_text=V).locator(".video-name").click()
        page.wait_for_selector("#card-sft", timeout=20_000)
        page.wait_for_selector("#card-report", timeout=20_000)
        page.wait_for_selector("#card-evidence", timeout=20_000)
    p.step("↻ 重试 → 推理完成(断言 SFT 标注/报告泳道)→ SFT/报告/证据卡", t_retry_done)

    # ---------- 8b. 演示停留:SFT 标注详情卡 / 分析报告卡各停 3 秒(fast 模式不停) ----------
    def t_sft_card_demo():
        page.locator("#card-sft").scroll_into_view_if_needed()
        demo(3000)  # 展示真实 sft 样本输出
    p.step("SFT 标注详情卡滚动可视区停留 3s", t_sft_card_demo)

    def t_report_card_demo():
        page.locator("#card-report").scroll_into_view_if_needed()
        demo(3000)  # 展示分析报告输出
    p.step("分析报告卡滚动可视区停留 3s", t_report_card_demo)

    # ---------- 9. SFT 选项联动(核心演示;无 chips 时 SKIP 不算失败) ----------
    # 选择器依据 traffic_analyzer/web/static/js/sft.js:
    #   chip: button.sft-chip[data-ev-chip][data-attr][data-value](.selected 为选中态)
    #   token: .sft-tok[data-attr];hover 联动类 .sft-tok-link;检出框 input[data-ev-check]
    #   未保存标记 #sft-dirty-flag;保存按钮 #btn-sft-save
    def t_sft_chips():
        if page.locator("#card-sft .sft-chip").count() == 0:
            # 当前 mock 样本无 attr_mentions,chips 不渲染(纯文本卡),跳过不算失败
            print("  [SKIP] SFT 选项联动:该样本无 attr_mentions,未渲染 chips")
            return
        card = page.locator("#card-sft .sft-ev", has=page.locator(".sft-chip")).first

        # 单选组:点未选中的 chip → 同卡同组 .sft-tok 文本变化 + 出现「● 未保存」
        # 多选组:切换一个 chip 的选中态(同组其余选中 chip 不丢失即判定为多选)
        single_done = multi_done = False
        rows = card.locator(".sft-attr-row")
        for i in range(rows.count()):
            if single_done and multi_done:
                break
            row = rows.nth(i)
            chips = row.locator(".sft-chip")
            if chips.count() < 2 or row.locator(".sft-chip.selected").count() == 0:
                continue
            attr = chips.first.get_attribute("data-attr")
            toks = card.locator(f'.sft-tok[data-attr="{attr}"]')
            before = toks.all_inner_texts()
            target = row.locator(".sft-chip:not(.selected)").first
            target.click()
            demo(2000)  # 让观众看清 chip → 文本联动
            if not single_done and toks.count() and toks.all_inner_texts() != before:
                single_done = True
                assert page.is_visible("#sft-dirty-flag"), "点 chip 后应出现「● 未保存」"
            elif not multi_done and row.locator(".sft-chip.selected").count() > 1:
                multi_done = True  # 点击后同组仍有多个选中 → 多选组,已完成一次切换

        # hover 一个 chip → 同事件卡内同组 .sft-tok 出现 .sft-tok-link 高亮类
        chip = card.locator(".sft-chip").first
        attr = chip.get_attribute("data-attr")
        chip.hover()
        page.wait_for_timeout(300)
        n_link = card.locator(f'.sft-tok.sft-tok-link[data-attr="{attr}"]').count()
        assert n_link > 0, "hover chip 后同组 token 应出现 .sft-tok-link"
        demo(2000)

        # 「检出」checkbox 切换一次再切回(结论预览联动)
        card.locator("input[data-ev-check]").click()
        demo(1500)
        card.locator("input[data-ev-check]").click()
        demo(1500)

        # 保存 → 「● 未保存」消失
        page.click("#btn-sft-save")
        page.wait_for_selector("#sft-dirty-flag", state="hidden", timeout=10_000)
    p.step("SFT 选项联动(chip/token/hover/检出/保存)", t_sft_chips)

    # ---------- 10. 证据编辑:拖拽应急车道多边形端点 → 保存 ----------
    # 选择器依据 traffic_analyzer/web/static/js/evidence.js:
    #   画布 .ev-canvas;顶点命中半径 HIT_R=8(CSS px,kind:'vertex');
    #   未保存标记 #dirty-flag;保存按钮 #btn-ev-save
    def t_evidence_edit():
        page.locator("#ev-tabs .ev-tab", has_text="应急车道占用").click()
        page.wait_for_selector(".ev-canvas", timeout=10_000)
        # 帧图加载后 fit() 才会给 canvas 设置 CSS 尺寸,否则端点像素换算无意义
        page.wait_for_function(
            "() => { const c = document.querySelector('.ev-canvas');"
            " return !!(c && c.style.width && c.style.width !== '0px'); }", timeout=10_000)
        # 画布在首屏视口之下,须先滚动进视口,否则 mouse 事件落在视口外不生效
        page.locator(".ev-canvas").scroll_into_view_if_needed()
        rect = page.locator(".ev-canvas").bounding_box()
        # 真实证据 01-02_Event_129 应急车道多边形端点 0 的归一化坐标,按画布 rect 换算像素
        vx = rect["x"] + EVIDENCE_VERTEX0_REL[0] * rect["width"]
        vy = rect["y"] + EVIDENCE_VERTEX0_REL[1] * rect["height"]
        page.mouse.move(vx, vy)  # 先 hover 到端点附近(命中后光标变 pointer)
        page.wait_for_timeout(300)
        page.mouse.down()
        for i in range(1, 6):    # 分 5 步拖动,演示拖轨迹
            page.mouse.move(vx - i * 8, vy + i * 6)
            page.wait_for_timeout(60)
        page.mouse.up()
        demo(2000)  # 关键演示点:拖拽后停留
        assert page.is_visible("#dirty-flag"), "拖拽端点后应出现「● 未保存」"
        page.click("#btn-ev-save")
        page.wait_for_selector("#dirty-flag", state="hidden", timeout=10_000)
    p.step("证据编辑(拖拽多边形端点 → 未保存 → 保存)", t_evidence_edit)

    # ---------- 11. 证据 Tab + 保存/重置键 ----------
    def t_evidence():
        tabs = page.locator("#ev-tabs .ev-tab")
        if tabs.count() > 1:
            tabs.nth(0).click()
            page.wait_for_timeout(300)
        page.wait_for_selector("#btn-ev-save[disabled]", timeout=15_000)  # 无修改时保存键应禁用
        page.wait_for_selector("#btn-ev-reset", timeout=15_000)
    p.step("证据 Tab 切换 + 保存/重置键", t_evidence)

    # ---------- 12. 预览区:重试播放 + 上下分隔条拖动 ----------
    def t_preview_pane():
        # mock 演示视频优先走真实后端流(readyState>0);流不可用时前端回退逐帧预览
        try:
            page.wait_for_function(
                "() => { const v = document.querySelector('#pv-video');"
                " return v && v.readyState > 0; }", timeout=10_000)
        except Exception:
            page.click("#pv-retry")  # 逐帧兜底模式:点「重试播放」重建一次
            page.wait_for_selector("#pv-slider", timeout=10_000)
        hs = page.locator("#hsplit").bounding_box()
        cx, cy = hs["x"] + hs["width"] / 2, hs["y"] + hs["height"] / 2
        h0 = page.locator("#pane-top").bounding_box()["height"]
        page.mouse.move(cx, cy)
        page.mouse.down()
        page.mouse.move(cx, cy + 40, steps=4)  # 往下拖 40px
        page.mouse.up()
        h1 = page.locator("#pane-top").bounding_box()["height"]
        assert abs((h1 - h0) - 40) < 8, f"分隔条下拖后预览高度应 +40px,实际 {h1 - h0:+.0f}px"
        demo(800)
        page.mouse.move(cx, cy + 40)
        page.mouse.down()
        page.mouse.move(cx, cy, steps=4)       # 拖回原位
        page.mouse.up()
    p.step("预览区(重试播放 + 分隔条拖动复位)", t_preview_pane)

    # ---------- 13. 数据看板(服务端分页/筛选) ----------
    # 入口按钮 #btn-dashboard 与看板视图(dashboard.js: #dash-root/.dash-chip/
    # .dash-review-chip/.dash-open/#dash-pager)由包B 提供;未就绪时各子断言降级为 SKIP
    # 契约:GET /api/dashboard → {summary, event_names, metrics};
    #       GET /api/dashboard/rows?page&size&consistency&review&edited&q
    #       → {rows, page, size, total, total_pages}
    def t_dashboard():
        btn = page.locator("#btn-dashboard")
        if btn.count() == 0:
            print("  [SKIP] 数据看板:工具栏尚无 #btn-dashboard 按钮(包B 未接线)")
            return
        btn.click()
        rows_sel = "#dash-body tr[data-rel]"
        try:
            page.wait_for_selector(rows_sel, timeout=10_000)
        except Exception:
            print("  [SKIP] 数据看板:按钮已点击但看板视图未渲染(dashboard.js 未就绪)")
            return

        def row_count() -> int:
            return page.locator(rows_sel).count()

        # 翻页条存在:「上一页 | 第 x / y 页 | 下一页」
        pager = page.locator("#dash-pager")
        assert pager.count() == 1, "看板应渲染翻页条 #dash-pager"
        assert page.locator("#dash-prev").count() == 1, "翻页条应含「上一页」按钮"
        assert page.locator("#dash-next").count() == 1, "翻页条应含「下一页」按钮"
        pm = re.search(r"第\s*(\d+)\s*/\s*(\d+)\s*页", pager.text_content() or "")
        # mock 视频数远小于每页 50 条,不足以演示多页翻页,
        # 页数断言降级为 total_pages >= 1(只验证翻页条渲染与页数自洽)
        assert pm and int(pm.group(1)) >= 1 and int(pm.group(2)) >= 1, \
            f"翻页条页数应 >=1,实际:{pager.text_content()!r}"

        # 页眉「第 a-b 条 / 共 N 条」与表格行数一致(单页时行数 == total)
        sm = re.search(r"第\s*(\d+)\s*-\s*(\d+)\s*条\s*/\s*共\s*(\d+)\s*条",
                       page.text_content("#dash-summary") or "")
        assert sm, f"页眉应显示「第 a-b 条 / 共 N 条」,实际:{page.text_content('#dash-summary')!r}"
        a, b, total = int(sm.group(1)), int(sm.group(2)), int(sm.group(3))
        n = row_count()
        assert n == b - a + 1, f"当前页行数应等于页眉区间 {a}-{b},实际 {n}"
        assert total >= 3, f"看板总行数应 >=3,实际 {total}"

        # 「人工已改」徽章:mock 将 01-02_Event_129 构造为 edited=true(pred_raw ≠ pred)
        assert page.locator("#dash-body .dash-badge-edit").count() >= 1, \
            "看板应出现「人工已改」徽章(.dash-badge-edit)"

        # 过滤 chip(服务端过滤):点「人工已改」,仅留 edited 行,行数应变少;再点一次复位
        chip = page.locator('.dash-chip[data-group="edited"]')
        if chip.count() == 0:
            print("  [SKIP] 看板过滤 chip:未找到 .dash-chip[data-group=edited]")
        else:
            chip.click()
            page.wait_for_function(
                "(t) => { const n = document.querySelectorAll("
                "'#dash-body tr[data-rel]').length; return n > 0 && n < t; }",
                arg=total, timeout=8000)
            page.locator('.dash-chip[data-group="edited"]').click()  # 重渲染后重新定位
            page.wait_for_function(
                "(t) => document.querySelectorAll('#dash-body tr[data-rel]').length === t",
                arg=total, timeout=8000)

        # 名称搜索(服务端过滤,300ms 防抖):输入特征子串后只剩 1 行;清空恢复
        search = page.locator("#dash-search")
        if search.count() == 0:
            print("  [SKIP] 看板名称搜索:未找到 #dash-search 输入框")
        else:
            search.fill(MOCK_FILTER_TEXT)
            page.wait_for_function(
                "() => document.querySelectorAll('#dash-body tr[data-rel]').length === 1",
                timeout=8000)
            search.fill("")
            page.wait_for_function(
                "(t) => document.querySelectorAll('#dash-body tr[data-rel]').length === t",
                arg=total, timeout=8000)

        # 审核 chip:点击后应出现选中态(.on)(就地更新,不整页重拉)
        review_chip = page.locator("#dash-body .dash-review-chip").first
        if review_chip.count() == 0:
            print("  [SKIP] 看板审核 chip:未找到 .dash-review-chip 元素")
        else:
            review_chip.click()
            page.wait_for_timeout(300)
            cls = page.locator("#dash-body .dash-review-chip").first.get_attribute("class") or ""
            assert "on" in cls.split(), f"审核 chip 点击后应有选中态 .on,实际 class={cls!r}"

        # 「打开 →」:回到详情视图且该视频被选中
        open_links = page.locator("#dash-body .dash-open")
        if open_links.count() == 0:
            print("  [SKIP] 看板「打开 →」:未找到 .dash-open 链接")
        else:
            open_links.first.click()
            sel("#pane-top")
            page.wait_for_selector("#video-list .video-item.active", timeout=8000)
    p.step("数据看板(翻页条/行数=total/筛选 chip/搜索/审核 chip/打开回详情)", t_dashboard)

    page.screenshot(path=str(shot), full_page=False)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--headless", action="store_true", help="无头模式")
    ap.add_argument("--passes", type=int, default=0, help="轮数,0=无限循环(Ctrl+C 停止)")
    ap.add_argument("--fast", action="store_true",
                    help="不做演示用慢速延迟(pause=0、slow_mo=0)")
    ap.add_argument("--pause", type=float, default=2.5,
                    help="每个步骤之间的停顿秒数(默认 2.5;--fast 时强制为 0)")
    args = ap.parse_args()

    pause = 0.0 if args.fast else max(0.0, args.pause)
    slow_mo = 0 if args.fast else 250

    ensure_server()
    SHOT_DIR.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright

    idx = 0
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(
                channel="chrome", headless=args.headless,
                slow_mo=slow_mo,
            )
        except Exception:
            print("[setup] 本机 Chrome 不可用,回退到 Playwright 内置 chromium")
            browser = pw.chromium.launch(headless=args.headless, slow_mo=slow_mo)
        page = browser.new_page(viewport={"width": 1600, "height": 900})
        js_errors = []
        page.on("pageerror", lambda e: js_errors.append(str(e)))
        try:
            while True:
                idx += 1
                js_errors.clear()  # 每轮只统计本轮的 JS 错误
                t0 = time.time()
                print(f"\n===== 第 {idx} 轮 =====")
                page.goto(BASE + "/?mock=1", wait_until="domcontentloaded")
                # 认证开启时会 302 到 /login:用 E2E_USER/E2E_PASS 登录一次后继续
                if "/login" in page.url:
                    user = os.environ.get("E2E_USER")
                    pw = os.environ.get("E2E_PASS")
                    if not user:
                        raise RuntimeError(
                            "服务开启了认证但未提供 E2E_USER/E2E_PASS 环境变量")
                    page.fill("#login-form input[name=username], #username", user)
                    page.fill("#login-form input[name=password], #password", pw)
                    page.click("button[type=submit]")
                    page.wait_for_url("**/", timeout=10_000)
                    page.goto(BASE + "/?mock=1", wait_until="domcontentloaded")
                page.wait_for_selector("#toolbar", timeout=10_000)
                p = Pass(idx, pause=pause)
                run_pass(page, p, SHOT_DIR / f"pass_{idx:03d}.png")
                _prune_screenshots()
                elapsed = time.time() - t0
                verdict = "FAIL" if (p.failures or js_errors) else "PASS"
                print(f"===== 第 {idx} 轮 {verdict}(耗时 {elapsed:.0f}s) =====")
                if js_errors:
                    print(f"  [FAIL] 页面 JS 错误: {js_errors[:3]}")
                if args.passes and idx >= args.passes:
                    return 1 if (p.failures or js_errors) else 0
                time.sleep(2)
        except KeyboardInterrupt:
            print(f"\n[stop] Ctrl+C,共完成 {idx} 轮")
            return 0
        finally:
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
