"""CLI entry point: ``python3 -m traffic_analyzer.toolserver``.

[文件说明]
作用:解析 --workspace / --port(环境变量 TRAFFIC_ANALYZER_WORKSPACE /
    TOOLSERVER_PORT 可作默认值),以 127.0.0.1 回环地址启动 uvicorn。
上游:命令行 / TS agent 运行时按需拉起。
下游:toolserver.create_app;uvicorn。
"""

from __future__ import annotations

import argparse
import os
import sys

_DEFAULT_PORT = 8601


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python3 -m traffic_analyzer.toolserver",
        description="Local video CV tool server for the TS agent runtime.",
    )
    parser.add_argument(
        "--workspace",
        default=os.environ.get("TRAFFIC_ANALYZER_WORKSPACE"),
        help="Path-safety root; every video_path must resolve inside it "
        "(env: TRAFFIC_ANALYZER_WORKSPACE).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("TOOLSERVER_PORT", str(_DEFAULT_PORT))),
        help=f"Listen port (env: TOOLSERVER_PORT, default {_DEFAULT_PORT}).",
    )
    args = parser.parse_args()
    if not args.workspace:
        parser.error(
            "--workspace is required (or set TRAFFIC_ANALYZER_WORKSPACE)"
        )

    import uvicorn

    from traffic_analyzer.toolserver import create_app

    app = create_app(args.workspace)
    # Loopback only: this service is consumed by the local TS agent runtime.
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    sys.exit(main())
