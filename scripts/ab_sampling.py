#!/usr/bin/env python3
"""采样参数 A/B 实验驱动:直连独立 agent server 实例跑一次「分析视频」检测。

用法:
    python3 scripts/ab_sampling.py <agent_base_url> <video_filename> <label> <out_dir>

- 创建 yolo 会话(免审批),POST /chat SSE 流式收集事件直到 done。
- 产出 <out_dir>/<label>_<video>.jsonl(全部事件)与 <out_dir>/<label>_<video>.summary.json。
"""
import json
import sys
import time
from pathlib import Path

import httpx

WORKSPACE = "/media/wanji/Elements/大模型应用/traffic-agent-local-version/演示区"


def find_key(obj, key):
    """递归查找字典/列表中的 key,返回第一个命中值。"""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            hit = find_key(v, key)
            if hit is not None:
                return hit
    elif isinstance(obj, list):
        for item in obj:
            hit = find_key(item, key)
            if hit is not None:
                return hit
    return None


def main() -> None:
    base, video, label, out_dir = sys.argv[1:5]
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{label}_{Path(video).stem}"

    t0 = time.time()
    events = []
    done = None
    error = None
    session_id = None
    try:
        with httpx.Client(base_url=base, timeout=httpx.Timeout(900.0, connect=10.0)) as client:
            resp = client.post("/sessions", json={"workspaceDir": WORKSPACE, "mode": "yolo"})
            if resp.status_code != 200:
                raise RuntimeError(f"POST /sessions -> {resp.status_code}: {resp.text[:500]}")
            session_id = resp.json()["sessionId"]
            with client.stream(
                "POST",
                "/chat",
                json={"sessionId": session_id, "input": "分析视频", "videoPath": video},
            ) as stream:
                if stream.status_code != 200:
                    stream.read()
                    raise RuntimeError(
                        f"POST /chat -> {stream.status_code}: {stream.text[:500]}"
                    )
                for line in stream.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload:
                        continue
                    try:
                        ev = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    ev["_t"] = round(time.time() - t0, 1)
                    events.append(ev)
                    if ev.get("type") == "done":
                        done = ev
                        break
    except Exception as exc:  # noqa: BLE001 - 实验脚本,记录一切失败
        error = f"{type(exc).__name__}: {exc}"

    wall_s = round(time.time() - t0, 1)
    (out / f"{stem}.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events),
        encoding="utf-8",
    )

    submit = None
    tool_calls = []
    for ev in events:
        if ev.get("type") == "tool_result":
            name = ev.get("name")
            tool_calls.append({"name": name, "t": ev.get("_t"), "isError": ev.get("isError")})
            if name == "submit_detection":
                submit = ev
        elif ev.get("type") == "structured_payload":
            submit = submit or ev

    summary = {
        "label": label,
        "video": video,
        "agent_base_url": base,
        "session_id": session_id,
        "wall_s": wall_s,
        "done_reason": (done or {}).get("reason"),
        "done_error": (done or {}).get("error"),
        "driver_error": error,
        "submit_ok": submit is not None,
        "binary_encoding": find_key(submit or {}, "binary_encoding"),
        "events_detected": [
            e.get("event_id")
            for e in (find_key(submit or {}, "events") or [])
            if isinstance(e, dict) and e.get("detected")
        ] if submit else None,
        "tool_calls": tool_calls,
        "n_events": len(events),
    }
    (out / f"{stem}.summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
