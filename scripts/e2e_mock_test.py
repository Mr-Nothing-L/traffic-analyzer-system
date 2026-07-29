#!/usr/bin/env python3
"""mock 模式全按键循环自测脚本。

用法(项目根目录):
    python3 scripts/e2e_mock_test.py              # 有头浏览器,无限循环,Ctrl+C 停止
    python3 scripts/e2e_mock_test.py --headless   # 无头模式
    python3 scripts/e2e_mock_test.py --passes 3   # 只跑 3 轮
    python3 scripts/e2e_mock_test.py --fast       # 不做慢速演示延迟

行为:自动确保 web 服务(127.0.0.1:8600)在运行(不在则启动,退出时不杀);
打开 ?mock=1 页面,对界面所有按键/页面做一轮完整遍历(工作区弹窗、过滤、
排序、全选、勾选、预览、推理、停止、重试、完成、证据 Tab、评估),打印每项
PASS/FAIL;不 Ctrl+C 就一直循环。每轮结束截图存到 output/e2e_screenshots/。
"""

import argparse
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8600"
SHOT_DIR = REPO_ROOT / "output" / "e2e_screenshots"


def server_up() -> bool:
    try:
        urllib.request.urlopen(BASE + "/", timeout=2)
        return True
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
    """一轮遍历的步骤记录器。"""

    def __init__(self, idx: int) -> None:
        self.idx = idx
        self.failures = []

    def step(self, name: str, fn) -> None:
        try:
            fn()
            print(f"  [PASS] {name}")
        except Exception as exc:  # noqa: BLE001 - 测试脚本要兜住一切继续跑
            self.failures.append(name)
            print(f"  [FAIL] {name}: {type(exc).__name__}: {str(exc)[:160]}")


def run_pass(page, p: Pass, shot: Path) -> None:
    """对 mock 页面做一轮全按键/全页面遍历。page 已打开 ?mock=1。"""
    V = "03_Event_102"  # mock 里用于推理流程的视频名片段

    def sel(s):
        return page.wait_for_selector(s, timeout=8000)

    # ---------- 1. 工作区弹窗 ----------
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

    # ---------- 2. 侧栏:过滤 / 排序 / 全选 ----------
    def t_filter_sort():
        page.fill("#side-filter-input", "03")
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

    # ---------- 3. 目录展开/折叠 + 视频预览 ----------
    def t_tree_preview():
        row = page.locator("#video-list .video-item", has_text=V)
        row.locator("input[data-check]").check()
        row.locator(".video-name").click()
        sel("#pane-top")
    p.step("勾选视频 + 点开预览", t_tree_preview)

    # ---------- 4. 推理 → 专家面板 → 停止 ----------
    def t_infer_stop():
        row = page.locator("#video-list .video-item", has_text=V)
        page.click("#btn-infer")
        sel("#card-experts")
        sel("#exp-lanes .expert-lane")
        page.wait_for_timeout(2500)  # 让泳道跑起来
        page.click("#exp-stop")
        row.locator(".badge.st-failed").wait_for(timeout=15_000)
    p.step("开始推理 → 专家工作间 → ■ 停止", t_infer_stop)

    # ---------- 5. ↻ 重试 → 等完成 → 结果页 ----------
    def t_retry_done():
        row = page.locator("#video-list .video-item", has_text=V)
        row.locator(".retry-btn").click()
        sel("#card-experts")
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
    p.step("↻ 重试 → 推理完成 → SFT/报告/证据卡", t_retry_done)

    # ---------- 6. 证据 Tab + 编辑按钮 ----------
    def t_evidence():
        tabs = page.locator("#ev-tabs .ev-tab")
        if tabs.count() > 1:
            tabs.nth(1).click()
            page.wait_for_timeout(300)
            tabs.nth(0).click()
        page.wait_for_selector("#btn-ev-save[disabled]", timeout=15_000)  # 无修改时保存键应禁用
        page.wait_for_selector("#btn-ev-reset", timeout=15_000)
    p.step("证据 Tab 切换 + 保存/重置键", t_evidence)

    # ---------- 7. 精度评估 ----------
    def t_eval():
        page.click("#btn-eval-run")
        page.wait_for_selector(".eval-table", timeout=60_000)
    p.step("运行评估 → 评估表", t_eval)

    # ---------- 8. 工具栏「精度评估」按钮 ----------
    def t_eval_toolbar():
        page.click("#btn-evaluate")
        page.wait_for_timeout(300)
    p.step("工具栏「精度评估」按钮", t_eval_toolbar)

    page.screenshot(path=str(shot), full_page=False)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--headless", action="store_true", help="无头模式")
    ap.add_argument("--passes", type=int, default=0, help="轮数,0=无限循环(Ctrl+C 停止)")
    ap.add_argument("--fast", action="store_true", help="不做演示用慢速延迟")
    args = ap.parse_args()

    ensure_server()
    SHOT_DIR.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright

    idx = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            channel="chrome", headless=args.headless,
            slow_mo=0 if args.fast else 120,
        )
        page = browser.new_page(viewport={"width": 1600, "height": 900})
        js_errors = []
        page.on("pageerror", lambda e: js_errors.append(str(e)))
        try:
            while True:
                idx += 1
                print(f"\n===== 第 {idx} 轮 =====")
                page.goto(BASE + "/?mock=1", wait_until="domcontentloaded")
                page.wait_for_selector("#toolbar", timeout=10_000)
                p = Pass(idx)
                run_pass(page, p, SHOT_DIR / f"pass_{idx:03d}.png")
                verdict = "FAIL" if (p.failures or js_errors) else "PASS"
                print(f"===== 第 {idx} 轮 {verdict} =====")
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
