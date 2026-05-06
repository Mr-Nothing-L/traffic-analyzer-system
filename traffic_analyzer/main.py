"""Main entry point for the traffic analyzer framework.

Usage:
    python -m traffic_analyzer analyze --video path/to/video.mp4
    python -m traffic_analyzer validate-config
"""

from __future__ import annotations

import sys

from traffic_analyzer.cli import main

if __name__ == "__main__":
    sys.exit(main())
