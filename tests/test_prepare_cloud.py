"""Deployment preparation uses synthetic manifests and never imports private .env data."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def exporter(monkeypatch, tmp_path):
    manifest = {
        "vector_store_id": "vs-private-test-index",
        "documents": {
            "drive-test-document": {
                "openai_file_id": "file-test-active",
                "name": "Private document title",
            }
        },
        "managed_file_ids": ["file-test-active", "file-unused-local"],
        "pending": {},
        "cleanup": {},
        "update_intents": {},
        "store_creation_pending": False,
    }
    settings = SimpleNamespace(
        drive_folder_id="folder-test-private",
        drive_include_subfolders=False,
        openai_model="test-model",
        openai_api_key="sk-test-must-not-be-exported",
    )
    validated = []

    class FakeSyncService:
        @staticmethod
        def load_manifest():
            return deepcopy(manifest)

        @staticmethod
        def _validate_manifest(snapshot):
            validated.append(deepcopy(snapshot))

    # Stub both imports before loading the script so config.load_dotenv never runs.
    fake_config = ModuleType("app.core.config")
    fake_config.settings = settings
    fake_sync = ModuleType("app.services.sync_service")
    fake_sync.SyncService = FakeSyncService
    monkeypatch.setitem(sys.modules, "app.core.config", fake_config)
    monkeypatch.setitem(sys.modules, "app.services.sync_service", fake_sync)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    script = Path(__file__).resolve().parents[1] / "scripts" / "prepare_cloud.py"
    spec = importlib.util.spec_from_file_location("prepare_cloud_isolated_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    return SimpleNamespace(module=module, manifest=manifest, root=tmp_path,
                           validated=validated, settings=settings)


def test_keeps_existing_access_key_and_exports_only_active_files(exporter, monkeypatch, capsys):
    destination = exporter.root / ".deployment"
    destination.mkdir()
    existing_key = "test-existing-private-access-key-12345"
    key_file = destination / "access-key.txt"
    key_file.write_text(existing_key, encoding="utf-8")

    def unexpected_rotation(_):
        pytest.fail("The existing key must not be rotated without the explicit option")

    monkeypatch.setattr(exporter.module.secrets, "token_urlsafe", unexpected_rotation)
    config = exporter.module.prepare_cloud()
    snapshot = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    saved_config = json.loads((destination / "vercel-env.json").read_text(encoding="utf-8"))

    assert key_file.read_text(encoding="utf-8") == existing_key
    assert config == saved_config
    assert config["APP_ACCESS_KEY"] == existing_key
    assert config["DEPLOYMENT_MODE"] == "cloud"
    assert config["GOOGLE_DRIVE_FOLDER_ID"] == exporter.settings.drive_folder_id
    assert config["DRIVE_INCLUDE_SUBFOLDERS"] == "false"
    assert "OPENAI_API_KEY" not in config
    assert snapshot["managed_file_ids"] == ["file-test-active"]
    assert json.loads(config["DOCUMENT_MANIFEST_JSON"]) == snapshot
    assert exporter.validated == [snapshot]
    assert exporter.manifest["managed_file_ids"] == ["file-test-active", "file-unused-local"]

    captured = capsys.readouterr()
    for private_value in (existing_key, exporter.settings.openai_api_key,
                          "Private document title", "vs-private-test-index",
                          exporter.settings.drive_folder_id):
        assert private_value not in captured.out
        assert private_value not in captured.err


@pytest.mark.parametrize("pending_field", [
    "pending", "cleanup", "update_intents", "store_creation_pending",
])
def test_blocks_unfinished_synchronization_before_writing(exporter, pending_field, capsys):
    exporter.manifest[pending_field] = True if pending_field == "store_creation_pending" else {"unfinished": {}}
    with pytest.raises(ValueError, match="Termina o reconcilia"):
        exporter.module.prepare_cloud()
    assert not (exporter.root / ".deployment").exists()
    assert not exporter.validated
    assert capsys.readouterr().out == ""


def test_rotates_only_when_requested(exporter, monkeypatch):
    destination = exporter.root / ".deployment"
    destination.mkdir()
    (destination / "access-key.txt").write_text("old-private-access-key-12345", encoding="utf-8")
    new_key = "new-private-access-key-67890"
    monkeypatch.setattr(exporter.module.secrets, "token_urlsafe", lambda size: new_key)
    config = exporter.module.prepare_cloud(rotate_access_key=True)
    assert config["APP_ACCESS_KEY"] == new_key
    assert (destination / "access-key.txt").read_text(encoding="utf-8") == new_key


def test_creates_key_when_none_exists(exporter, monkeypatch):
    new_key = "new-private-access-key-12345"
    monkeypatch.setattr(exporter.module.secrets, "token_urlsafe", lambda size: new_key)
    assert exporter.module.prepare_cloud()["APP_ACCESS_KEY"] == new_key


def test_rejects_short_existing_key_without_replacing_it(exporter):
    destination = exporter.root / ".deployment"
    destination.mkdir()
    key_file = destination / "access-key.txt"
    key_file.write_text("short-key", encoding="utf-8")
    with pytest.raises(ValueError, match="al menos 24"):
        exporter.module.prepare_cloud()
    assert key_file.read_text(encoding="utf-8") == "short-key"
    assert not (destination / "vercel-env.json").exists()


@pytest.mark.parametrize("missing_field", ["documents", "vector_store_id"])
def test_requires_completed_index_with_documents(exporter, missing_field):
    exporter.manifest[missing_field] = {} if missing_field == "documents" else ""
    with pytest.raises(ValueError, match="Primero sincroniza"):
        exporter.module.prepare_cloud()
    assert not (exporter.root / ".deployment").exists()


def test_rejects_oversized_snapshot_before_writing(exporter):
    exporter.manifest["documents"]["drive-test-document"]["name"] = "x" * 48000
    with pytest.raises(ValueError, match="supera el tamaño"):
        exporter.module.prepare_cloud()
    assert not (exporter.root / ".deployment").exists()
