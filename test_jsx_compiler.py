"""Tests for server-side module JSX bundling, ETag caching, and lazy map bundle decoupling."""
import os
from fastapi.testclient import TestClient

import backend.api.server as server
from backend.engine.jsx_compiler import build_widget_bundle

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))


def test_build_widget_bundle_walks_relative_imports_and_compiles(tmp_path):
    modules_dir = tmp_path / "modules"
    mod_dir = modules_dir / "wb_sample"
    (mod_dir / "sub").mkdir(parents=True)
    (mod_dir / "widget.jsx").write_text(
        "import React from 'react';\n"
        "import { label } from './sub/helper';\n"
        "export default function Sample() { return <div>{label()}</div>; }\n",
        encoding="utf-8",
    )
    (mod_dir / "sub" / "helper.js").write_text(
        "export function label() { return 'ok'; }\n",
        encoding="utf-8",
    )

    etag, bundle = build_widget_bundle(str(modules_dir), REPO_ROOT, "wb_sample", "widget.jsx")
    assert etag
    assert bundle["entry"] == "widget"
    assert set(bundle["files"].keys()) == {"widget", "sub/helper"}
    assert bundle["files"]["widget"]["deps"] == {"./sub/helper": "sub/helper"}
    # Node + @babel/standalone is present in frontend/node_modules in this repo.
    assert bundle["files"]["widget"]["code"] is not None
    assert "exports" in bundle["files"]["widget"]["code"]


def test_build_widget_bundle_blocks_path_traversal(tmp_path):
    modules_dir = tmp_path / "modules"
    mod_dir = modules_dir / "wb_mod"
    mod_dir.mkdir(parents=True)
    outside = modules_dir / "secret.jsx"
    outside.write_text("export default () => null;\n", encoding="utf-8")

    try:
        build_widget_bundle(str(modules_dir), REPO_ROOT, "wb_mod", "../secret.jsx")
        assert False, "Expected FileNotFoundError for path traversal"
    except FileNotFoundError:
        pass


def test_widget_bundle_endpoint_etag_and_404():
    client = TestClient(server.app)

    res = client.get("/api/widgets/wb_core_rpg/bundle", params={"entry": "widget.jsx"})
    assert res.status_code == 200
    etag = res.headers.get("etag")
    assert etag
    data = res.json()
    assert data["entry"] == "widget"
    assert "widget" in data["files"]

    cached = client.get(
        "/api/widgets/wb_core_rpg/bundle",
        params={"entry": "widget.jsx"},
        headers={"If-None-Match": etag},
    )
    assert cached.status_code == 304

    missing = client.get("/api/widgets/wb_core_rpg/bundle", params={"entry": "does_not_exist.jsx"})
    assert missing.status_code == 404


def test_game_map_overlay_bundle_does_not_eagerly_pull_map_renderer():
    modules_dir = os.path.join(REPO_ROOT, "modules")
    _, overlay_bundle = build_widget_bundle(modules_dir, REPO_ROOT, "wb_worldgen", "ui/GameMapOverlay.jsx")
    assert "ui/GameMapOverlay" in overlay_bundle["files"]
    assert "ui/WorldBuilder/MapRenderer" not in overlay_bundle["files"]
