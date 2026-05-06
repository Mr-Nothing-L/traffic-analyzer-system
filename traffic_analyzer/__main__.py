"""Enable ``python -m traffic_analyzer``."""

from __future__ import annotations

import sys

from traffic_analyzer.cli import main

if __name__ == "__main__":
    sys.exit(main())
