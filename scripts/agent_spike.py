"""Tool-calling spike for local vLLM (qwen3.8-27b-fp8, qwen3_xml parser).

Self-contained: run from repo root with ``python3 scripts/agent_spike.py``.

Verifies OpenAI-compatible function calling reliability of the local vLLM
server (started with --enable-auto-tool-choice --tool-call-parser qwen3_xml):
  1. Loads provider config from .env via ConfigManager (index 0, aliyun type).
  2. Registers 3 local tools (video_meta / extract_frames / draw_boxes).
  3. Runs an agent loop (max 8 rounds) per test case, logging every tool
     call's name/args/result, JSON parse errors and param errors.
  4. Vision closure: frames returned by extract_frames are appended to the
     conversation as base64 image messages (max 2) so the model answers from
     real pixels.
  5. Three cases: Q1 (should call tools), Q2 (multi-tool chain), Q3 (common
     sense, must NOT call tools).
  6. Prints a structured report at the end.
"""

from __future__ import annotations

import base64
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402
import openai  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from traffic_analyzer.core.config_manager import ConfigManager  # noqa: E402
from traffic_analyzer.web.frames import read_frame_jpeg, read_video_meta  # noqa: E402

SPIKE_DIR = Path("/tmp/spike_frames")
SPIKE_DIR.mkdir(parents=True, exist_ok=True)

MAX_ROUNDS = 8
# The model thinks before answering; reasoning tokens share this budget.
# 4096 was exhausted by a single reasoning burst after image input
# (finish=length, empty content) — use the provider's configured budget.
MAX_TOKENS = 8192
TEMPERATURE = 0.2

VIDEO_1 = str(REPO_ROOT / "演示区" / "01-02_Event_129_1755579215119_1.mp4")
VIDEO_2 = str(REPO_ROOT / "演示区" / "01-02-04_Event_2048_1750664210002_1.mp4")


# ---------------------------------------------------------------------------
# Config / client
# ---------------------------------------------------------------------------

def build_client() -> Tuple[openai.OpenAI, str]:
    """Load .env provider index 0 and build an OpenAI SDK client."""
    cm = ConfigManager(str(REPO_ROOT / "traffic_analyzer" / "config"))
    providers = cm._load_env_llm_providers()
    cfg = providers[0]
    print(f"[config] provider={cfg.provider} base_url={cfg.base_url} "
          f"model={cfg.model} timeout={cfg.timeout}")
    # Bypass system proxies (httpx does not support socks:// by default),
    # same convention as vlm_engine._init_client_for_provider.
    http_client = httpx.Client(proxy=None, trust_env=False, timeout=cfg.timeout)
    base_url = cfg.base_url or "https://dashscope.aliyuncs.com/compatible-mode/v1"
    client = openai.OpenAI(
        api_key=cfg.api_key, base_url=base_url, http_client=http_client
    )
    return client, cfg.model


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def tool_video_meta(video_path: str) -> Dict[str, Any]:
    meta = read_video_meta(Path(video_path))
    if meta is None:
        return {"error": f"cannot open video: {video_path}"}
    return meta


def tool_extract_frames(
    video_path: str, start_sec: float, end_sec: float, max_frames: int = 4
) -> Dict[str, Any]:
    meta = read_video_meta(Path(video_path))
    if meta is None:
        return {"error": f"cannot open video: {video_path}"}
    fps = meta["fps"]
    total = meta["frame_count"]
    start_idx = max(0, int(start_sec * fps))
    end_idx = min(total - 1, int(end_sec * fps))
    if end_idx < start_idx:
        return {"error": f"empty range: start_sec={start_sec} end_sec={end_sec}"}
    n = max(1, min(int(max_frames), 8))
    if n == 1:
        indices = [start_idx]
    else:
        step = (end_idx - start_idx) / (n - 1)
        indices = sorted({round(start_idx + i * step) for i in range(n)})
    paths, timestamps = [], []
    stem = Path(video_path).stem
    for idx in indices:
        data = read_frame_jpeg(Path(video_path), idx)
        if data is None:
            continue
        out = SPIKE_DIR / f"{stem}_f{idx}.jpg"
        out.write_bytes(data)
        paths.append(str(out))
        timestamps.append(round(idx / fps, 2))
    return {"paths": paths, "timestamps": timestamps, "fps": fps}


def tool_draw_boxes(
    image_path: str, boxes: List[List[float]], labels: List[str]
) -> Dict[str, Any]:
    img = Image.open(image_path).convert("RGB")
    w, h = img.size
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 22)
    except Exception:
        font = ImageFont.load_default()
    for i, box in enumerate(boxes):
        if len(box) != 4:
            return {"error": f"box {i} must have 4 numbers, got {box}"}
        x1, y1, x2, y2 = box
        # normalized xyxy -> pixels (clamp to image)
        px1, py1 = max(0, x1 * w), max(0, y1 * h)
        px2, py2 = min(w, x2 * w), min(h, y2 * h)
        label = labels[i] if i < len(labels) else f"obj{i}"
        draw.rectangle([px1, py1, px2, py2], outline=(255, 40, 40), width=4)
        draw.text((px1 + 4, max(0, py1 - 26)), label, fill=(255, 40, 40), font=font)
    out = SPIKE_DIR / f"annotated_{Path(image_path).name}"
    img.save(out)
    return {"output_path": str(out), "n_boxes": len(boxes)}


# ---------------------------------------------------------------------------
# Tool schemas + dispatch with validation
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "video_meta",
            "description": "获取视频文件的元信息:时长(秒)、fps、总帧数、分辨率。",
            "parameters": {
                "type": "object",
                "properties": {
                    "video_path": {"type": "string", "description": "视频文件绝对路径"},
                },
                "required": ["video_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_frames",
            "description": (
                "从视频中按时间范围抽帧,保存为 JPEG 文件,返回文件路径和对应时间戳(秒)。"
                "start_sec/end_sec 为秒,会自动换算为帧号。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "video_path": {"type": "string", "description": "视频文件绝对路径"},
                    "start_sec": {"type": "number", "description": "起始秒"},
                    "end_sec": {"type": "number", "description": "结束秒"},
                    "max_frames": {
                        "type": "integer",
                        "description": "最多抽取帧数,默认 4",
                        "default": 4,
                    },
                },
                "required": ["video_path", "start_sec", "end_sec"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "draw_boxes",
            "description": (
                "在图片上绘制归一化 xyxy 边界框(0~1 浮点)并标注标签,"
                "返回标注后图片的输出路径。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "image_path": {"type": "string", "description": "输入图片路径"},
                    "boxes": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 4,
                            "maxItems": 4,
                        },
                        "description": "归一化 xyxy 框列表,如 [[0.1,0.2,0.3,0.4]]",
                    },
                    "labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "与 boxes 对应的标签",
                    },
                },
                "required": ["image_path", "boxes", "labels"],
            },
        },
    },
]

# name -> (func, {param: python type}, required params)
_TOOL_IMPLS = {
    "video_meta": (
        tool_video_meta,
        {"video_path": str},
        {"video_path"},
    ),
    "extract_frames": (
        tool_extract_frames,
        {"video_path": str, "start_sec": (int, float), "end_sec": (int, float),
         "max_frames": int},
        {"video_path", "start_sec", "end_sec"},
    ),
    "draw_boxes": (
        tool_draw_boxes,
        {"image_path": str, "boxes": list, "labels": list},
        {"image_path", "boxes", "labels"},
    ),
}


class CallRecord(dict):
    """One tool-call attempt log entry."""


def execute_tool(name: str, raw_args: str) -> Tuple[str, CallRecord]:
    """Validate + run one tool call. Returns (result_json, record)."""
    rec = CallRecord(
        name=name,
        raw_args=raw_args,
        status="ok",
        error=None,
        result=None,
    )
    impl = _TOOL_IMPLS.get(name)
    if impl is None:
        rec["status"] = "unknown_tool"
        rec["error"] = f"unknown tool: {name}"
        return json.dumps({"error": rec["error"]}, ensure_ascii=False), rec

    func, type_map, required = impl
    try:
        args = json.loads(raw_args) if raw_args else {}
        if not isinstance(args, dict):
            raise ValueError(f"arguments is not a JSON object: {type(args).__name__}")
    except Exception as exc:
        rec["status"] = "json_parse_error"
        rec["error"] = f"{type(exc).__name__}: {exc}"
        return json.dumps({"error": f"invalid JSON arguments: {exc}"},
                          ensure_ascii=False), rec
    rec["parsed_args"] = args

    missing = [p for p in required if p not in args]
    if missing:
        rec["status"] = "param_error"
        rec["error"] = f"missing required params: {missing}"
        return json.dumps({"error": rec["error"]}, ensure_ascii=False), rec

    for key, val in args.items():
        expected = type_map.get(key)
        if expected is None:
            continue  # tolerate unknown extra params
        if expected is int and isinstance(val, bool):
            pass
        elif expected is int and isinstance(val, float) and val.is_integer():
            args[key] = int(val)  # tolerate 4.0 for int params
        elif not isinstance(val, expected):
            rec["status"] = "param_error"
            rec["error"] = (f"param '{key}' type error: expected {expected}, "
                            f"got {type(val).__name__} ({val!r:.120})")
            return json.dumps({"error": rec["error"]}, ensure_ascii=False), rec

    try:
        result = func(**args)
    except Exception as exc:
        rec["status"] = "exec_error"
        rec["error"] = f"{type(exc).__name__}: {exc}"
        rec["traceback"] = traceback.format_exc(limit=3)
        return json.dumps({"error": f"tool execution failed: {exc}"},
                          ensure_ascii=False), rec

    rec["result"] = result
    return json.dumps(result, ensure_ascii=False), rec


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

def image_data_url(path: str) -> str:
    data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{data}"


def run_case(client: openai.OpenAI, model: str, case_id: str,
             question: str, expect_tools: bool) -> Dict[str, Any]:
    print(f"\n{'=' * 72}\n[{case_id}] {question}\n{'=' * 72}")
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": (
            "你是一个高速公路视频分析助手。需要视频信息或画面时,必须调用提供的工具,"
            "不要凭空臆测视频内容。基于工具返回的真实数据作答,用中文回答。")},
        {"role": "user", "content": question},
    ]
    report: Dict[str, Any] = {
        "case_id": case_id,
        "question": question,
        "expect_tools": expect_tools,
        "rounds": 0,
        "calls": [],
        "n_calls": 0,
        "n_ok": 0,
        "n_json_parse_error": 0,
        "n_param_error": 0,
        "n_unknown_tool": 0,
        "n_exec_error": 0,
        "xml_leak_in_content": 0,
        "protocol_notes": [],
        "final_answer": None,
    }
    sent_images: set = set()
    t0 = time.time()

    for rnd in range(1, MAX_ROUNDS + 1):
        report["rounds"] = rnd
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                max_tokens=MAX_TOKENS,
                temperature=TEMPERATURE,
            )
        except Exception as exc:
            report["protocol_notes"].append(
                f"round {rnd}: request raised {type(exc).__name__}: {exc}")
            print(f"  [round {rnd}] REQUEST ERROR: {type(exc).__name__}: {exc}")
            break

        msg = resp.choices[0].message
        finish = resp.choices[0].finish_reason
        content = msg.content or ""
        reasoning = getattr(msg, "reasoning_content", None) or getattr(
            msg, "reasoning", None)
        tool_calls = msg.tool_calls or []

        # Reliability signal: qwen3_xml markup leaking into plain content
        # means the parser failed to lift the call into tool_calls.
        if "<tool_call" in content or "<function=" in content:
            report["xml_leak_in_content"] += 1
            report["protocol_notes"].append(
                f"round {rnd}: tool-call XML leaked into content: {content[:400]!r}")

        print(f"  [round {rnd}] finish={finish} tool_calls={len(tool_calls)} "
              f"content_len={len(content)} reasoning_len={len(reasoning or '')}")
        if reasoning:
            print(f"    reasoning: {str(reasoning)[:160]!r}...")
        if content and not tool_calls:
            print(f"    content: {content[:300]}")

        # Append assistant message (manual serialization to keep protocol clean)
        assistant_msg: Dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name,
                                 "arguments": tc.function.arguments},
                }
                for tc in tool_calls
            ]
        messages.append(assistant_msg)

        if not tool_calls:
            report["final_answer"] = content
            break

        new_frame_paths: List[str] = []
        for tc in tool_calls:
            name = tc.function.name
            raw_args = tc.function.arguments or ""
            print(f"    -> call {name} args={raw_args[:240]!r}")
            result_str, rec = execute_tool(name, raw_args)
            report["calls"].append(rec)
            report["n_calls"] += 1
            if rec["status"] == "ok":
                report["n_ok"] += 1
            else:
                key = f"n_{rec['status']}"
                report[key] = report.get(key, 0) + 1
                print(f"       [{rec['status']}] {rec['error']}")
                if rec["status"] == "json_parse_error":
                    print(f"       raw_args: {raw_args[:400]!r}")
            if rec["status"] == "ok":
                preview = result_str[:300]
                print(f"       result: {preview}")
                if name == "extract_frames":
                    try:
                        new_frame_paths.extend(
                            json.loads(result_str).get("paths", []))
                    except Exception:
                        pass
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "name": name,
                "content": result_str,
            })

        # Vision closure: feed up to 2 freshly extracted frames back as images
        fresh = [p for p in new_frame_paths if p not in sent_images][:2]
        if fresh:
            sent_images.update(fresh)
            messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": (
                        "下面是从视频中抽取的真实画面,请仔细观察后基于画面内容继续分析作答。")},
                    *[{"type": "image_url",
                       "image_url": {"url": image_data_url(p)}} for p in fresh],
                ],
            })
            print(f"    [vision] appended {len(fresh)} frame image(s) to conversation")

    report["elapsed_sec"] = round(time.time() - t0, 1)
    if report["final_answer"] is None and not report["protocol_notes"]:
        report["protocol_notes"].append("hit MAX_ROUNDS without final answer")
    return report


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_report(reports: List[Dict[str, Any]], total_elapsed: float) -> None:
    print(f"\n{'#' * 72}\n# SPIKE REPORT\n{'#' * 72}")
    for r in reports:
        ok = r["n_ok"]
        total = r["n_calls"]
        rate = f"{ok}/{total} ({100.0 * ok / total:.0f}%)" if total else "0/0 (n/a)"
        triggered = "YES" if total else "NO"
        expect = "tools expected" if r["expect_tools"] else "no tools expected"
        verdict = "PASS" if (total > 0) == r["expect_tools"] else "FAIL"
        print(f"\n--- {r['case_id']} [{verdict}] ({expect}, triggered={triggered}) ---")
        print(f"  rounds          : {r['rounds']}")
        print(f"  tool calls      : {total} (success {rate})")
        print(f"  json_parse_err  : {r['n_json_parse_error']}")
        print(f"  param_err       : {r['n_param_error']}")
        print(f"  unknown_tool    : {r['n_unknown_tool']}")
        print(f"  exec_err        : {r['n_exec_error']}")
        print(f"  xml_leak        : {r['xml_leak_in_content']}")
        print(f"  elapsed         : {r['elapsed_sec']}s")
        for note in r["protocol_notes"]:
            print(f"  NOTE: {note}")
        for c in r["calls"]:
            status = c["status"]
            line = f"    - {c['name']} [{status}]"
            if status != "ok":
                line += f" error={c['error']}"
            print(line)
        ans = r["final_answer"] or "<none>"
        print(f"  final answer    : {ans[:400]}")
    total_calls = sum(r["n_calls"] for r in reports)
    total_ok = sum(r["n_ok"] for r in reports)
    print(f"\n=== TOTALS ===")
    print(f"  tool calls      : {total_calls}, success {total_ok}"
          + (f" ({100.0 * total_ok / total_calls:.0f}%)" if total_calls else ""))
    print(f"  json_parse_err  : {sum(r['n_json_parse_error'] for r in reports)}")
    print(f"  param_err       : {sum(r['n_param_error'] for r in reports)}")
    print(f"  xml_leak        : {sum(r['xml_leak_in_content'] for r in reports)}")
    print(f"  total elapsed   : {total_elapsed:.1f}s")

    # Persist machine-readable report
    out = REPO_ROOT / "output" / "agent_spike_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(reports, ensure_ascii=False, indent=2, default=str))
    print(f"  report json     : {out}")


def main() -> None:
    client, model = build_client()
    t0 = time.time()
    reports = [
        run_case(
            client, model, "Q1",
            f"这个视频里有没有车辆占用应急车道?请先用工具了解视频信息,"
            f"再抽取关键时刻的画面检查。视频路径:{VIDEO_1}",
            expect_tools=True,
        ),
        run_case(
            client, model, "Q2",
            f"视频后半段有没有行人或非机动车?找到的话把目标在图上框出来。"
            f"视频路径:{VIDEO_1}(也可参考 {VIDEO_2})",
            expect_tools=True,
        ),
        run_case(
            client, model, "Q3",
            "高速公路上应急车道的法定用途是什么?",
            expect_tools=False,
        ),
    ]
    print_report(reports, time.time() - t0)


if __name__ == "__main__":
    main()
