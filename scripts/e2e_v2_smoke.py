#!/usr/bin/env python3
"""v2 前端(Vue 3,frontend/dist)真实后端端到端冒烟脚本。

用法(项目根目录):
    python3 scripts/e2e_v2_smoke.py                # 无头 Chrome,默认 127.0.0.1:8608
    python3 scripts/e2e_v2_smoke.py --headed       # 有头浏览器
    python3 scripts/e2e_v2_smoke.py --port 8609 --video-fragment 01-02_Event_129

行为:自建临时账号(写入 users.db,结束后删除)、临时工作区(--workspace-src
的副本,视频用符号链接),在 127.0.0.1:<port> 启动真实后端(认证开启,密钥经
TRAFFIC_ANALYZER_SECRET 环境注入,不写 config/.env),用 Chrome 跑一遍:
未认证跳转登录页 → 登录 → 加载工作区 → 侧栏树渲染 → 视频详情(预览/报告/
证据卡渲染)→ SFT 编辑器渲染 → 数据看板渲染 → 登出。每步打印 PASS/FAIL,
单步失败不中断整轮,任一步失败退出码非零。截图存到
output/e2e_screenshots/v2_smoke_*.png。

注:不跑真实推理(太重,需 VLM/GPU)与 SSE 进度推送;这两块由
traffic_analyzer/tests/web/ 的后端测试覆盖。
"""

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from traffic_analyzer.web import user_store  # noqa: E402

SHOT_DIR = REPO_ROOT / "output" / "e2e_screenshots"
SMOKE_USERNAME = "v2_smoke_user"
SMOKE_PASSWORD = "v2_smoke_pass"
SMOKE_SECRET = "v2-smoke-secret"  # 仅注入测试实例环境,避免写回 config/.env

RESULTS: list = []  # (步骤名, 是否通过, 备注)


def record(name: str, ok: bool, note: str = "") -> bool:
    RESULTS.append((name, ok, note))
    suffix = f" ({note})" if note else ""
    print(f"{'PASS' if ok else 'FAIL'}  {name}{suffix}", flush=True)
    return ok


def server_up(base: str) -> bool:
    """应用指纹校验:认证开启时 /api/jobs 401 即证明是本系统的服务在监听。"""
    try:
        with urllib.request.urlopen(base + "/api/jobs", timeout=2) as resp:
            return resp.status == 200 and isinstance(json.loads(resp.read()), list)
    except urllib.error.HTTPError as e:
        return e.code == 401
    except Exception:
        return False


def start_backend(port: int, workspace: Path) -> subprocess.Popen:
    """启动真实后端(不跑 CLI web 子命令:它会自动打开浏览器)。"""
    import os

    env = dict(os.environ)
    env["TRAFFIC_ANALYZER_WEB_WORKSPACE"] = str(workspace)
    env["TRAFFIC_ANALYZER_SECRET"] = SMOKE_SECRET
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "traffic_analyzer.web.app:create_app", "--factory",
            "--host", "127.0.0.1", "--port", str(port),
        ],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc


def run_smoke(base: str, video_fragment: str, headless: bool) -> None:
    from playwright.sync_api import sync_playwright

    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=headless)
        page = browser.new_page()
        page.set_default_timeout(15000)
        try:
            # 1. 未认证访问 / → 服务端 302 到登录页
            try:
                page.goto(base + "/", wait_until="domcontentloaded")
                page.wait_for_selector(".login-card")
                record("未认证 / 跳转登录页", "/login" in page.url)
            except Exception as e:
                record("未认证 / 跳转登录页", False, str(e))

            # 2. 登录 → 工作台顶栏出现
            try:
                page.fill("#login-username", SMOKE_USERNAME)
                page.fill("#login-password", SMOKE_PASSWORD)
                page.click(".login-btn")
                page.wait_for_selector(".app-topbar")
                record("登录", True)
            except Exception as e:
                record("登录", False, str(e))
                raise  # 登录失败后续步骤无意义

            # 3. 加载工作区(欢迎卡显式按钮,同 legacy)
            try:
                page.click("button:has-text('加载工作区')")
                page.wait_for_selector(".video-list .video-item")
                page.screenshot(path=str(SHOT_DIR / "v2_smoke_workspace.png"))
                record("加载工作区", True)
            except Exception as e:
                record("加载工作区", False, str(e))
                raise

            # 4. 侧栏树渲染
            n_items = page.locator(".video-list .video-item").count()
            record("侧栏树渲染", n_items > 0, f"{n_items} 个视频条目")

            # 5. 点开视频详情:预览/报告/证据卡渲染
            try:
                page.click(f".video-list .video-item:has-text('{video_fragment}')")
                page.wait_for_selector(".card-preview")
                page.wait_for_selector(".card-report .report-body")
                page.wait_for_selector(".card-evidence")
                has_video = page.locator(".card-preview video").count() > 0
                page.screenshot(path=str(SHOT_DIR / "v2_smoke_detail.png"))
                record("视频详情渲染(预览/报告/证据卡)", has_video,
                       "" if has_video else "预览卡无 <video> 元素")
            except Exception as e:
                record("视频详情渲染(预览/报告/证据卡)", False, str(e))

            # 6. SFT 编辑器渲染
            n_sft = page.locator(".sft-ev").count()
            record("SFT 编辑器渲染", n_sft > 0, f"{n_sft} 个事件卡")

            # 7. 数据看板渲染(深链直达,顺带验证 SPA 深链)
            try:
                page.goto(base + "/dashboard", wait_until="domcontentloaded")
                page.wait_for_selector(".dash-title")
                page.wait_for_selector(".dash-card")
                page.screenshot(path=str(SHOT_DIR / "v2_smoke_dashboard.png"))
                record("数据看板渲染", True)
            except Exception as e:
                record("数据看板渲染", False, str(e))

            # 8. 登出 → 回到登录页
            try:
                page.click(".user-avatar")
                page.click(".user-pop-logout")
                page.wait_for_selector(".login-card")
                record("登出", "/login" in page.url)
            except Exception as e:
                record("登出", False, str(e))
        finally:
            browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8608, help="测试实例端口(默认 %(default)s)")
    parser.add_argument("--workspace-src", default=str(REPO_ROOT / "演示区"),
                        help="临时工作区的复制来源(默认 %(default)s)")
    parser.add_argument("--video-fragment", default="01-02_Event_129",
                        help="详情步骤要打开的视频名片段(默认 %(default)s)")
    parser.add_argument("--headed", action="store_true", help="有头浏览器(默认无头)")
    args = parser.parse_args()

    base = f"http://127.0.0.1:{args.port}"
    if server_up(base):
        print(f"error: {base} 已被占用,请换 --port", file=sys.stderr)
        return 2

    # 临时账号 + 临时工作区(视频符号链接,避免复制大文件)
    user_store.remove_user(SMOKE_USERNAME)  # 清理上次异常退出的残留
    if not user_store.add_user(SMOKE_USERNAME, SMOKE_PASSWORD):
        print(f"error: 无法创建临时账号 {SMOKE_USERNAME}", file=sys.stderr)
        return 2
    workspace = Path(tempfile_mkdtemp())
    shutil.rmtree(workspace)
    shutil.copytree(args.workspace_src, workspace, symlinks=True)

    proc = start_backend(args.port, workspace)
    try:
        deadline = time.time() + 30
        while time.time() < deadline and not server_up(base):
            if proc.poll() is not None:
                print("error: 后端进程启动后退出", file=sys.stderr)
                return 2
            time.sleep(0.3)
        if not server_up(base):
            print(f"error: 后端 30s 内未就绪({base})", file=sys.stderr)
            return 2
        print(f"后端已就绪:{base}  工作区副本:{workspace}")
        run_smoke(base, args.video_fragment, headless=not args.headed)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        user_store.remove_user(SMOKE_USERNAME)
        shutil.rmtree(workspace, ignore_errors=True)

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} 步通过"
          + (f",失败: {', '.join(r[0] for r in failed)}" if failed else ""))
    return 1 if failed else 0


def tempfile_mkdtemp() -> str:
    import tempfile

    return tempfile.mkdtemp(prefix="v2_smoke_ws_")


if __name__ == "__main__":
    sys.exit(main())
