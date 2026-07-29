"""Web CLI subcommand and static-asset cache-header tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from traffic_analyzer.cli import build_parser, main
from traffic_analyzer.web.app import create_app


# ---------------------------------------------------------------------------
# CLI: web subcommand
# ---------------------------------------------------------------------------


class TestCliWeb:
    def test_web_help(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(["web", "--help"])
        assert exc_info.value.code == 0

    def test_web_defaults(self) -> None:
        args = build_parser().parse_args(["web"])
        assert args.command == "web"
        assert args.host == "127.0.0.1"
        assert args.port == 8600
        assert args.workspace is None

    def test_web_custom_args(self) -> None:
        args = build_parser().parse_args(["web", "--host", "0.0.0.0", "--port", "9000", "-w", "/tmp/ws"])
        assert args.host == "0.0.0.0"
        assert args.port == 9000
        assert args.workspace == "/tmp/ws"

    def test_cmd_web_invalid_workspace(self, tmp_path: Path) -> None:
        from traffic_analyzer.cli import cmd_web

        args = build_parser().parse_args(["web", "--workspace", str(tmp_path / "nope")])
        assert cmd_web(args) == 1
# ---------------------------------------------------------------------------
# Static asset cache headers
# ---------------------------------------------------------------------------


class TestStaticCacheHeaders:
    """SPA assets must always revalidate so upgrades don't break on stale cache."""

    def test_index_and_assets_send_no_cache(self) -> None:
        client = TestClient(create_app())
        for path in ("/", "/js/main.js", "/js/sft.js", "/style.css"):
            resp = client.get(path)
            assert resp.status_code == 200, path
            assert resp.headers.get("cache-control") == "no-cache", path

    def test_api_responses_not_affected(self) -> None:
        client = TestClient(create_app())
        resp = client.get("/api/jobs")
        assert resp.status_code == 200
        assert "cache-control" not in resp.headers
