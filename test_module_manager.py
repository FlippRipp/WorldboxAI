"""App-wide module manager: registry state (enable/disable/install/remove),
zip/GitHub install paths, and the /api/module-manager endpoints."""
import io
import json
import os
import zipfile

import pytest
from fastapi.testclient import TestClient

import backend.api.server as server
from backend.engine.registry import (
    ModuleManagerError,
    ModuleRegistry,
    github_archive_url,
    parse_github_url,
)


def manifest_dict(mod_id, dependencies=None, **extra):
    return {
        "id": mod_id,
        "name": extra.pop("name", mod_id),
        "version": "1.0.0",
        "dependencies": dependencies or [],
        "consumes": {"state": ["turn"], "module_data": [], "module_configs": [], "world_data": False},
        "produces": {"module_data": False, "context_string": False, "messages": False},
        **extra,
    }


def write_module(modules_dir, folder, mod_id, dependencies=None, backend="marker = True\n"):
    path = modules_dir / folder
    path.mkdir(parents=True)
    (path / "manifest.json").write_text(json.dumps(manifest_dict(mod_id, dependencies)))
    (path / "backend.py").write_text(backend)


def make_registry(tmp_path):
    modules_dir = tmp_path / "modules"
    modules_dir.mkdir(exist_ok=True)
    registry = ModuleRegistry(str(modules_dir), state_path=str(tmp_path / "modules_state.json"))
    return registry, modules_dir


def build_zip(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def module_zip_entries(mod_id, prefix="", dependencies=None, backend="marker = True\n"):
    p = f"{prefix}/" if prefix else ""
    return {
        f"{p}manifest.json": json.dumps(manifest_dict(mod_id, dependencies)),
        f"{p}backend.py": backend,
    }


# ---------------------------------------------------------------------------
# Registry: enable / disable


def test_disabled_module_is_discovered_but_not_loaded(tmp_path):
    registry, modules_dir = make_registry(tmp_path)
    write_module(modules_dir, "alpha", "wb_alpha")
    write_module(modules_dir, "beta", "wb_beta")
    (tmp_path / "modules_state.json").write_text(json.dumps({"disabled": ["wb_beta"], "installed": []}))

    registry._load_state()
    registry.load_all_modules()

    assert "wb_alpha" in registry.get_modules()
    assert "wb_beta" not in registry.get_modules()
    listing = {entry["id"]: entry for entry in registry.manager_listing()}
    assert listing["wb_beta"]["enabled"] is False
    assert listing["wb_beta"]["loaded"] is False
    assert listing["wb_beta"]["load_error"] is None
    assert listing["wb_alpha"]["enabled"] is True
    assert listing["wb_alpha"]["loaded"] is True
    assert listing["wb_alpha"]["builtin"] is True


def test_disable_enable_roundtrip_persists(tmp_path):
    registry, modules_dir = make_registry(tmp_path)
    write_module(modules_dir, "alpha", "wb_alpha")
    registry.load_all_modules()

    registry.disable_module("wb_alpha")
    assert "wb_alpha" not in registry.get_modules()

    # A fresh registry (server restart) sees the persisted disable.
    restarted = ModuleRegistry(str(modules_dir), state_path=str(tmp_path / "modules_state.json"))
    restarted.load_all_modules()
    assert "wb_alpha" not in restarted.get_modules()

    restarted.enable_module("wb_alpha")
    assert "wb_alpha" in restarted.get_modules()
    assert getattr(restarted.get_modules()["wb_alpha"]["backend"], "marker", False) is True

    again = ModuleRegistry(str(modules_dir), state_path=str(tmp_path / "modules_state.json"))
    again.load_all_modules()
    assert "wb_alpha" in again.get_modules()


def test_disable_refused_while_loaded_dependent_exists(tmp_path):
    registry, modules_dir = make_registry(tmp_path)
    write_module(modules_dir, "base", "wb_base")
    write_module(modules_dir, "child", "wb_child", dependencies=["wb_base"])
    registry.load_all_modules()

    with pytest.raises(ModuleManagerError) as exc:
        registry.disable_module("wb_base")
    assert exc.value.status == 409
    assert "wb_child" in str(exc.value)

    registry.disable_module("wb_child")
    registry.disable_module("wb_base")
    assert registry.get_modules() == {}


def test_enable_refused_until_dependency_enabled(tmp_path):
    registry, modules_dir = make_registry(tmp_path)
    write_module(modules_dir, "base", "wb_base")
    write_module(modules_dir, "child", "wb_child", dependencies=["wb_base"])
    (tmp_path / "modules_state.json").write_text(
        json.dumps({"disabled": ["wb_base", "wb_child"], "installed": []})
    )
    registry._load_state()
    registry.load_all_modules()

    with pytest.raises(ModuleManagerError) as exc:
        registry.enable_module("wb_child")
    assert exc.value.status == 409
    assert "wb_base" in str(exc.value)

    registry.enable_module("wb_base")
    registry.enable_module("wb_child")
    assert set(registry.get_modules()) == {"wb_base", "wb_child"}


def test_state_prunes_vanished_modules(tmp_path):
    registry, modules_dir = make_registry(tmp_path)
    write_module(modules_dir, "alpha", "wb_alpha")
    (tmp_path / "modules_state.json").write_text(
        json.dumps({"disabled": ["wb_ghost"], "installed": ["wb_ghost2"]})
    )
    registry._load_state()
    registry.load_all_modules()

    saved = json.loads((tmp_path / "modules_state.json").read_text())
    assert saved == {"disabled": [], "installed": []}


def test_enable_unknown_module_404(tmp_path):
    registry, _ = make_registry(tmp_path)
    registry.load_all_modules()
    with pytest.raises(ModuleManagerError) as exc:
        registry.enable_module("wb_nope")
    assert exc.value.status == 404


def test_broken_backend_records_load_error(tmp_path):
    registry, modules_dir = make_registry(tmp_path)
    write_module(modules_dir, "broken", "wb_broken", backend="raise RuntimeError('boom')\n")
    registry.load_all_modules()

    assert "wb_broken" not in registry.get_modules()
    listing = {entry["id"]: entry for entry in registry.manager_listing()}
    assert "boom" in listing["wb_broken"]["load_error"]


# ---------------------------------------------------------------------------
# Registry: install / remove


def test_install_from_zip_root_and_remove(tmp_path):
    registry, modules_dir = make_registry(tmp_path)
    registry.load_all_modules()

    registry.install_module_from_zip(build_zip(module_zip_entries("wb_new")))

    assert "wb_new" in registry.get_modules()
    assert (modules_dir / "wb_new" / "backend.py").is_file()
    listing = {entry["id"]: entry for entry in registry.manager_listing()}
    assert listing["wb_new"]["builtin"] is False
    assert listing["wb_new"]["enabled"] is True

    registry.remove_module("wb_new")
    assert "wb_new" not in registry.get_modules()
    assert not (modules_dir / "wb_new").exists()
    saved = json.loads((tmp_path / "modules_state.json").read_text())
    assert saved == {"disabled": [], "installed": []}


def test_install_from_github_style_wrapped_zip(tmp_path):
    registry, _ = make_registry(tmp_path)
    registry.load_all_modules()

    # GitHub archives wrap content in a repo-branch/ top folder.
    registry.install_module_from_zip(build_zip(module_zip_entries("wb_wrapped", prefix="repo-main")))
    assert "wb_wrapped" in registry.get_modules()


def test_install_with_subpath_hint(tmp_path):
    registry, _ = make_registry(tmp_path)
    registry.load_all_modules()

    entries = {
        **module_zip_entries("wb_a", prefix="repo-main/modules/wb_a"),
        **module_zip_entries("wb_b", prefix="repo-main/modules/wb_b"),
    }
    # Without a subpath, two candidate roots is an error.
    with pytest.raises(ModuleManagerError, match="Multiple modules"):
        registry.install_module_from_zip(build_zip(entries))
    # The /tree/branch/path subpath resolves inside the wrapper folder.
    registry.install_module_from_zip(build_zip(entries), subpath="modules/wb_b")
    assert "wb_b" in registry.get_modules()
    assert "wb_a" not in registry.get_modules()


def test_install_rejects_invalid_manifest_without_residue(tmp_path):
    registry, modules_dir = make_registry(tmp_path)
    registry.load_all_modules()

    bad = dict(module_zip_entries("wb_bad"))
    bad["manifest.json"] = json.dumps({"id": "wb_bad", "name": "Bad", "version": "1.0.0"})  # no consumes/produces
    with pytest.raises(ModuleManagerError, match="Invalid manifest"):
        registry.install_module_from_zip(build_zip(bad))
    assert not (modules_dir / "wb_bad").exists()
    assert "wb_bad" not in registry.discovered


def test_install_rolls_back_when_backend_import_fails(tmp_path):
    registry, modules_dir = make_registry(tmp_path)
    registry.load_all_modules()

    with pytest.raises(ModuleManagerError):
        registry.install_module_from_zip(
            build_zip(module_zip_entries("wb_crash", backend="raise RuntimeError('no')\n"))
        )
    assert not (modules_dir / "wb_crash").exists()
    assert "wb_crash" not in registry.discovered
    saved = json.loads((tmp_path / "modules_state.json").read_text())
    assert "wb_crash" not in saved["installed"]


def test_install_rejects_duplicate_id(tmp_path):
    registry, modules_dir = make_registry(tmp_path)
    write_module(modules_dir, "alpha", "wb_alpha")
    registry.load_all_modules()

    with pytest.raises(ModuleManagerError) as exc:
        registry.install_module_from_zip(build_zip(module_zip_entries("wb_alpha")))
    assert exc.value.status == 409


def test_install_rejects_zip_slip_paths(tmp_path):
    registry, _ = make_registry(tmp_path)
    registry.load_all_modules()

    evil = dict(module_zip_entries("wb_evil"))
    evil["../outside.txt"] = "escaped"
    with pytest.raises(ModuleManagerError, match="unsafe path"):
        registry.install_module_from_zip(build_zip(evil))
    assert not (tmp_path / "outside.txt").exists()


def test_install_rejects_non_zip_bytes(tmp_path):
    registry, _ = make_registry(tmp_path)
    registry.load_all_modules()
    with pytest.raises(ModuleManagerError, match="valid zip"):
        registry.install_module_from_zip(b"definitely not a zip")


def test_remove_refused_for_builtin(tmp_path):
    registry, modules_dir = make_registry(tmp_path)
    write_module(modules_dir, "alpha", "wb_alpha")
    registry.load_all_modules()

    with pytest.raises(ModuleManagerError) as exc:
        registry.remove_module("wb_alpha")
    assert exc.value.status == 403
    assert (modules_dir / "alpha").exists()


def test_remove_refused_with_loaded_dependent(tmp_path):
    registry, modules_dir = make_registry(tmp_path)
    write_module(modules_dir, "child", "wb_child", dependencies=["wb_dep"])
    registry.load_all_modules()
    registry.install_module_from_zip(build_zip(module_zip_entries("wb_dep")))
    # wb_child failed at startup (missing dep); re-enable it now that wb_dep exists.
    registry.enable_module("wb_child")

    with pytest.raises(ModuleManagerError) as exc:
        registry.remove_module("wb_dep")
    assert exc.value.status == 409

    registry.disable_module("wb_child")
    registry.remove_module("wb_dep")
    assert "wb_dep" not in registry.discovered


# ---------------------------------------------------------------------------
# GitHub URL parsing


def test_parse_github_url_forms():
    assert parse_github_url("https://github.com/user/repo") == {
        "owner": "user", "repo": "repo", "branch": None, "subpath": None,
    }
    assert parse_github_url("https://github.com/user/repo.git")["repo"] == "repo"
    assert parse_github_url("https://www.github.com/user/repo/tree/dev") == {
        "owner": "user", "repo": "repo", "branch": "dev", "subpath": None,
    }
    assert parse_github_url("https://github.com/user/repo/tree/main/modules/wb_x") == {
        "owner": "user", "repo": "repo", "branch": "main", "subpath": "modules/wb_x",
    }


@pytest.mark.parametrize("url", [
    "https://gitlab.com/user/repo",
    "https://github.com/user",
    "https://github.com/user/repo/pulls",
    "ftp://github.com/user/repo",
    "not a url",
])
def test_parse_github_url_rejects(url):
    with pytest.raises(ModuleManagerError):
        parse_github_url(url)


def test_github_archive_url():
    assert github_archive_url("u", "r", None) == "https://codeload.github.com/u/r/zip/HEAD"
    assert github_archive_url("u", "r", "dev") == "https://codeload.github.com/u/r/zip/refs/heads/dev"


# ---------------------------------------------------------------------------
# API endpoints


def make_manager_client(tmp_path, monkeypatch):
    modules_dir = tmp_path / "modules"
    modules_dir.mkdir()
    write_module(modules_dir, "alpha", "wb_alpha")
    registry = ModuleRegistry(str(modules_dir), state_path=str(tmp_path / "modules_state.json"))
    registry.load_all_modules()
    monkeypatch.setattr(server, "registry", registry)
    return TestClient(server.app), registry


def test_manager_endpoints_toggle_and_listing(tmp_path, monkeypatch):
    client, _ = make_manager_client(tmp_path, monkeypatch)

    listing = client.get("/api/module-manager")
    assert listing.status_code == 200
    assert [m["id"] for m in listing.json()["modules"]] == ["wb_alpha"]

    off = client.put("/api/module-manager/wb_alpha/enabled", json={"enabled": False})
    assert off.status_code == 200
    assert off.json()["module"]["enabled"] is False

    # The game-facing module list no longer includes it.
    live = client.get("/api/modules")
    assert all(m["id"] != "wb_alpha" for m in live.json()["modules"])

    on = client.put("/api/module-manager/wb_alpha/enabled", json={"enabled": True})
    assert on.status_code == 200
    assert on.json()["module"]["loaded"] is True

    missing = client.put("/api/module-manager/wb_ghost/enabled", json={"enabled": False})
    assert missing.status_code == 404


def test_manager_endpoints_install_zip_and_remove(tmp_path, monkeypatch):
    import base64

    client, registry = make_manager_client(tmp_path, monkeypatch)

    payload = base64.b64encode(build_zip(module_zip_entries("wb_upload"))).decode("ascii")
    installed = client.post(
        "/api/module-manager/install",
        json={"source": "zip", "data_base64": payload, "filename": "wb_upload.zip"},
    )
    assert installed.status_code == 200
    entry = installed.json()["module"]
    assert entry["id"] == "wb_upload"
    assert entry["builtin"] is False
    assert "wb_upload" in registry.get_modules()

    removed = client.delete("/api/module-manager/wb_upload")
    assert removed.status_code == 200
    assert "wb_upload" not in registry.get_modules()

    builtin = client.delete("/api/module-manager/wb_alpha")
    assert builtin.status_code == 403


def test_manager_endpoints_install_github(tmp_path, monkeypatch):
    client, registry = make_manager_client(tmp_path, monkeypatch)

    async def fake_download(ref):
        assert ref["owner"] == "user" and ref["repo"] == "repo"
        return build_zip(module_zip_entries("wb_remote", prefix="repo-main"))

    monkeypatch.setattr(server, "_download_github_archive", fake_download)

    installed = client.post(
        "/api/module-manager/install",
        json={"source": "github", "url": "https://github.com/user/repo"},
    )
    assert installed.status_code == 200
    assert installed.json()["module"]["id"] == "wb_remote"
    assert "wb_remote" in registry.get_modules()

    bad_url = client.post(
        "/api/module-manager/install",
        json={"source": "github", "url": "https://example.com/user/repo"},
    )
    assert bad_url.status_code == 400

    bad_source = client.post("/api/module-manager/install", json={"source": "carrier-pigeon"})
    assert bad_source.status_code == 400
