"""Enable ``python -m traffic_analyzer``.

[文件说明]
作用:使 ``python -m traffic_analyzer`` 等价于调用 cli.main(),本身不含参数解析逻辑。
上游:命令行 ``python -m traffic_analyzer``;scripts/infer.sh、scripts/analyze.sh、
scripts/batch_infer.py 与 web/jobs.py 均以子进程形式经此进入 CLI。
下游:traffic_analyzer/cli.py 的 main()。
"""

from __future__ import annotations

import sys

from traffic_analyzer.cli import main

if __name__ == "__main__":
    sys.exit(main())
