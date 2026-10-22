#!/usr/bin/env python3
"""SFT Forge 前置探测:确认现有 provider 端点支持 video_url 视频直传输入。

对 .env 配置的第一个启用 provider 发一次最小 chat/completions 请求
(user content = video_url dataURI + 一句文本),能拿到非空文本回复即视为
端点支持视频输入;HTTP 4xx 或明确报 video 不支持则判定不支持,此时应停止
SFT Forge 开发并回报用户决策(不得擅自回退抽帧方案)。

用法(在主仓或 worktree 根目录下运行):
    python3 scripts/probe_sft_forge_video.py \
        --env-file /path/to/traffic_analyzer/config/.env \
        --video "演示区/01-02_Event_129_1755579215119_1.mp4"

安全约定:只打印 provider 名称 / base_url / 模型名与响应摘要,严禁打印
API key 或任何 secret。
"""

from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path

# 允许从仓库根目录直接运行(scripts/ 的上一级入 sys.path)。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from traffic_analyzer.core.config_manager import ConfigManager  # noqa: E402

_PROBE_TEXT = "这是一段高速公路监控视频。请用一句话回答:你是否能看到这段视频的画面内容?"


def _pick_default_video() -> Path:
    """缺省视频:当前目录 演示区/ 下体积最小的 mp4(探测流量最小化)。"""
    demo_dir = Path("演示区")
    candidates = sorted(demo_dir.glob("*.mp4"), key=lambda p: p.stat().st_size)
    if not candidates:
        raise SystemExit("未找到缺省探测视频:请用 --video 显式指定一个 mp4")
    return candidates[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        default="traffic_analyzer/config/.env",
        help="provider 配置 .env 路径(文件名必须为 .env;默认 traffic_analyzer/config/.env)",
    )
    parser.add_argument("--video", default=None, help="探测用 mp4;缺省取 演示区/ 下最小 mp4")
    args = parser.parse_args()

    env_file = Path(args.env_file)
    if not env_file.exists():
        print(f"[FAIL] env file 不存在: {env_file}")
        return 2
    if env_file.name != ".env":
        print("[FAIL] --env-file 必须指向名为 .env 的文件(provider 解析复用 ConfigManager)")
        return 2

    manager = ConfigManager(config_dir=str(env_file.parent))
    manager.load_all()
    providers = [p for p in manager.get_llm_providers()]
    if not providers:
        print("[FAIL] .env 中没有可用的 LLM provider 配置")
        return 2
    provider = providers[0]
    print(
        f"[INFO] provider={provider.provider} base_url={provider.base_url} "
        f"model={provider.model} timeout={provider.timeout}s"
    )

    video_path = Path(args.video) if args.video else _pick_default_video()
    if not video_path.exists():
        print(f"[FAIL] 视频不存在: {video_path}")
        return 2
    video_bytes = video_path.read_bytes()
    b64 = base64.b64encode(video_bytes).decode()
    print(
        f"[INFO] video={video_path.name} size={len(video_bytes) / 1e6:.1f}MB "
        f"b64={len(b64) / 1e6:.1f}MB"
    )

    try:
        import httpx
        import openai
    except ImportError as exc:
        print(f"[FAIL] 缺少依赖: {exc}")
        return 2

    client = openai.OpenAI(
        api_key=provider.api_key,
        base_url=provider.base_url,
        http_client=httpx.Client(proxy=None, trust_env=False, timeout=provider.timeout),
    )
    # qwen3 系思考模型可能把 token 预算耗在隐藏 reasoning 上导致 content 为空,
    # 故显式 enable_thinking=False 并给足 max_tokens,避免误判为不支持视频。
    payload = {
        "model": provider.model,
        "max_tokens": 512,
        "temperature": 0.0,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "video_url",
                        "video_url": {"url": f"data:video/mp4;base64,{b64}"},
                    },
                    {"type": "text", "text": _PROBE_TEXT},
                ],
            }
        ],
    }
    try:
        resp = client.chat.completions.create(**payload)
    except Exception as exc:  # noqa: BLE001
        # 只打印异常类型与消息摘要,绝不打印请求体(含 base64 与鉴权头)。
        print(f"[UNSUPPORTED] 请求失败: {type(exc).__name__}: {str(exc)[:400]}")
        return 1

    choice = resp.choices[0] if resp.choices else None
    text = (choice.message.content or "").strip() if choice else ""
    finish = getattr(choice, "finish_reason", None) if choice else None
    usage = getattr(resp, "usage", None)
    print(f"[INFO] finish_reason={finish} usage={usage}")
    if not text:
        print("[UNSUPPORTED] 端点返回空内容,无法确认视频输入支持")
        return 1
    print(f"[OK] 端点支持 video_url 视频输入。响应摘要: {text[:120]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
