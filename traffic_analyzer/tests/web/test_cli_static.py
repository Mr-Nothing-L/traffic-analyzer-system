"""Web CLI subcommand and static-asset cache-header tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from traffic_analyzer.cli import build_parser, main
from traffic_analyzer.web.app import create_app

_DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"
_requires_dist = pytest.mark.skipif(
    not (_DIST / "index.html").is_file(), reason="frontend/dist not built"
)


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


@_requires_dist
class TestStaticCacheHeaders:
    """SPA assets must always revalidate so upgrades don't break on stale cache."""

    def test_index_and_assets_send_no_cache(self) -> None:
        client = TestClient(create_app())
        # dist 的 assets 文件名带 hash,从 dist 目录实际探测一个真实资源。
        asset = next((_DIST / "assets").iterdir())
        paths = (
            "/",
            "/index.html",
            f"/assets/{asset.name}",
            "/fonts/fusion-pixel-12px.woff2",
        )
        for path in paths:
            resp = client.get(path)
            assert resp.status_code == 200, path
            assert resp.headers.get("cache-control") == "no-cache", path

    def test_api_responses_not_affected(self) -> None:
        client = TestClient(create_app())
        resp = client.get("/api/jobs")
        assert resp.status_code == 200
        assert "cache-control" not in resp.headers


# ---------------------------------------------------------------------------
# SPA routing:/ 由 frontend/dist 服务,深链回退 index.html,/v2 旧书签 301
# ---------------------------------------------------------------------------


@_requires_dist
class TestSpaRouting:
    def test_spa_deep_link_falls_back_to_index(self) -> None:
        client = TestClient(create_app())
        for path in ("/", "/login", "/dashboard", "/video/some-stem"):
            resp = client.get(path)
            assert resp.status_code == 200, path
            assert resp.headers["content-type"].startswith("text/html"), path

    def test_v2_bookmarks_redirect_301(self) -> None:
        client = TestClient(create_app())
        resp = client.get("/v2", follow_redirects=False)
        assert resp.status_code == 301
        assert resp.headers["location"] == "/"
        resp = client.get("/v2/dashboard", follow_redirects=False)
        assert resp.status_code == 301
        assert resp.headers["location"] == "/dashboard"
