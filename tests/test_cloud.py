"""Cloud access and immutable document snapshots, without external services."""
from dataclasses import replace
import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.core.config import Settings
from app.core.errors import StateError
from app.services import sync_service as sync

ACCESS_KEY = "private-access-test"
API_KEY = "private-openai-test"
HEADERS = {"X-App-Key": ACCESS_KEY, "X-Requested-With": "AgenteDocumentalTIC",
           "Content-Type": "application/json"}


@pytest.fixture
def cloud(monkeypatch, tmp_path):
    config = Settings(deployment_mode="cloud", app_access_key=ACCESS_KEY, openai_api_key=API_KEY,
                      drive_folder_id="authorized-folder-123", vector_store_id="vs-private",
                      google_credentials_path=tmp_path / "credentials.json",
                      google_token_path=tmp_path / "token.json")
    monkeypatch.setattr(sync, "settings", config)
    snapshot = sync.SyncService._new_manifest()
    snapshot.update(vector_store_id="vs-private", managed_file_ids=["file-private"])
    snapshot["documents"]["drive-source-123"] = {
        "drive_id": "drive-source-123", "name": "Manual.txt", "mime_type": "text/plain",
        "url": "https://drive.google.com/file/d/drive-source-123/view", "modified_time": None,
        "drive_checksum": None, "drive_version": None, "openai_file_id": "file-private",
        "content_sha256": "a" * 64}
    config = replace(config, document_manifest_json=json.dumps(snapshot))
    monkeypatch.setattr(main, "settings", config)
    monkeypatch.setattr(sync, "settings", config)
    main._requests.clear()
    with TestClient(main.app, base_url="https://agent.example", client=("203.0.113.8", 4333)) as client:
        yield client, config, snapshot


def test_vercel_mode_defaults_and_explicit_local_override(monkeypatch):
    monkeypatch.delenv("DEPLOYMENT_MODE", raising=False)
    monkeypatch.setenv("VERCEL", "1")
    assert Settings().is_cloud
    monkeypatch.setenv("DEPLOYMENT_MODE", "local")
    assert not Settings().is_cloud
    monkeypatch.setenv("DEPLOYMENT_MODE", "invalid")
    with pytest.raises(ValueError, match="DEPLOYMENT_MODE"):
        Settings()


def test_cloud_configuration_hides_secrets_in_repr(cloud):
    _, config, _ = cloud
    representation = repr(config)
    for secret in (ACCESS_KEY, API_KEY, config.document_manifest_json, "file-private"):
        assert secret not in representation


def test_public_health_is_ready_without_google_oauth_and_redacts_index(cloud):
    client, _, _ = cloud
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["configured"] is True and data["deployment_mode"] == "cloud"
    assert data["access_key_required"] is True and data["sync_available"] is False
    assert data["checks"]["access_key"] and data["checks"]["document_index"]
    assert not data["checks"]["google_credentials"] and not data["checks"]["oauth_token"]
    for private in (ACCESS_KEY, API_KEY, "vs-private", "file-private", "Manual.txt", "drive-source-123"):
        assert private not in response.text


@pytest.mark.parametrize("path,method", [("/api/documents", "get"), ("/api/ask", "post"), ("/api/sync", "post")])
def test_cloud_endpoints_require_access_before_using_providers(cloud, monkeypatch, path, method):
    client, _, _ = cloud
    service = Mock(side_effect=AssertionError("Provider must not be constructed"))
    monkeypatch.setattr(main, "AgentService", service)
    monkeypatch.setattr(main.SyncService, "load_manifest", service)
    request = getattr(client, method)
    for key in (None, "wrong-key"):
        headers = {name: value for name, value in HEADERS.items() if name != "X-App-Key"}
        if key is not None:
            headers["X-App-Key"] = key
        response = request(path, headers=headers)
        assert response.status_code == 401
    service.assert_not_called()


def test_missing_cloud_access_key_fails_closed(cloud, monkeypatch):
    client, config, _ = cloud
    monkeypatch.setattr(main, "settings", replace(config, app_access_key=""))
    assert client.get("/api/documents", headers=HEADERS).status_code == 503
    data = client.get("/api/health").json()
    assert not data["configured"] and not data["checks"]["access_key"]


def test_authenticated_remote_reads_and_queries_work(cloud, monkeypatch):
    client, _, _ = cloud
    response = client.get("/api/documents", headers=HEADERS)
    assert response.status_code == 200 and response.json()["indexed"] == 1
    assert response.json()["documents"][0]["name"] == "Manual.txt"
    assert "file-private" not in response.text and "vs-private" not in response.text
    service = Mock()
    service.ask.return_value = {"success": True, "answer": "Respuesta de prueba.", "sources": [], "grounded": False}
    monkeypatch.setattr(main, "AgentService", lambda: service)
    result = client.post("/api/ask", json={"question": "Pregunta válida"}, headers=HEADERS)
    assert result.status_code == 200
    service.ask.assert_called_once_with("Pregunta válida", True, [])


def test_cloud_keeps_same_origin_json_csrf_and_body_limits(cloud):
    client, _, _ = cloud
    assert client.get("/api/documents", headers=HEADERS | {"Origin": "https://elsewhere.example"}).status_code == 403
    assert client.get("/api/documents", headers=HEADERS | {"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get("/api/documents", headers=HEADERS | {"Origin": "https://agent.example"}).status_code == 200
    assert client.post("/api/ask", json={}, headers={"X-App-Key": ACCESS_KEY}).status_code == 403
    assert client.post("/api/ask", content="x", headers=HEADERS | {"Content-Type": "text/plain"}).status_code == 415
    assert client.post("/api/ask", json={"question": "x" * 40000}, headers=HEADERS).status_code == 413


@pytest.mark.parametrize("path", ["/", "/assets/js/app.js"])
def test_public_cloud_interface_opens_from_external_links(cloud, path):
    client, _, _ = cloud
    response = client.get(path, headers={"Origin": "https://elsewhere.example",
                                         "Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 200
    assert response.headers["X-Frame-Options"] == "DENY"
    assert ACCESS_KEY not in response.text and API_KEY not in response.text
    assert client.get("/api/documents", headers=HEADERS | {
        "Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_cloud_snapshot_reads_without_local_disk_and_rejects_writes(cloud, monkeypatch):
    _, _, snapshot = cloud
    # Any filesystem method would fail; this deployment has only an environment snapshot.
    monkeypatch.setattr(sync, "MANIFEST", object())
    assert sync.SyncService.load_manifest() == snapshot
    with pytest.raises(StateError, match="no guarda"):
        sync.SyncService.save_manifest(snapshot)


@pytest.mark.parametrize("raw", ["", "private-invalid-json", "[]"])
def test_missing_or_invalid_snapshot_never_leaks_its_content(cloud, monkeypatch, raw):
    client, config, _ = cloud
    modified = replace(config, document_manifest_json=raw)
    monkeypatch.setattr(main, "settings", modified)
    monkeypatch.setattr(sync, "settings", modified)
    with pytest.raises(StateError) as error:
        sync.SyncService.load_manifest()
    assert "private-invalid-json" not in str(error.value)
    response = client.get("/api/documents", headers=HEADERS)
    assert response.status_code == 409 and "private-invalid-json" not in response.text
    assert client.get("/api/health").json()["configured"] is False


@pytest.mark.parametrize("change", ["folder", "scope", "store", "empty"])
def test_cloud_snapshot_scope_and_readiness_are_validated(cloud, monkeypatch, change):
    client, config, snapshot = cloud
    if change == "folder":
        snapshot["folder_id"] = "another-folder-123"
    elif change == "scope":
        snapshot["scope_fingerprint"] = "wrong-scope"
    elif change == "store":
        snapshot["vector_store_id"] = "vs-another"
    else:
        snapshot["documents"] = {}
    modified = replace(config, document_manifest_json=json.dumps(snapshot))
    monkeypatch.setattr(main, "settings", modified)
    monkeypatch.setattr(sync, "settings", modified)
    assert client.get("/api/health").json()["configured"] is False
    if change != "empty":
        assert client.get("/api/documents", headers=HEADERS).status_code == 409


def test_cloud_sync_never_starts_oauth_or_provider_clients(cloud, monkeypatch):
    client, _, _ = cloud
    forbidden = Mock(side_effect=AssertionError("Cloud sync must not create providers"))
    monkeypatch.setattr(sync, "DriveService", forbidden)
    monkeypatch.setattr(sync, "get_openai_client", forbidden)
    response = client.post("/api/sync", json={}, headers=HEADERS)
    assert response.status_code == 409 and "local" in response.json()["detail"]
    with pytest.raises(StateError, match="local"):
        sync.SyncService().sync()
    forbidden.assert_not_called()
    assert not sync._LOCK.locked()
