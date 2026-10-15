from dataclasses import replace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.core.config import Settings

HEADERS = {"X-Requested-With": "AgenteDocumentalTIC", "Content-Type": "application/json"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    config = Settings(openai_api_key="", drive_folder_id="authorized-folder-123",
                      google_credentials_path=tmp_path / "credentials.json", google_token_path=tmp_path / "token.json")
    monkeypatch.setattr(main, "settings", config)
    monkeypatch.setattr(main.SyncService, "load_manifest", lambda: {"documents": {}, "vector_store_id": ""})
    main._requests.clear()
    with TestClient(main.app, base_url="http://127.0.0.1:5000", client=("127.0.0.1", 45000)) as instance:
        yield instance


def test_home_and_local_assets(client):
    r = client.get("/")
    assert r.status_code == 200 and "Agente de documentos" in r.text
    assert "cdn.jsdelivr" not in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert client.get("/assets/js/app.js").status_code == 200
    assert client.get("/.env").status_code == 404
    assert client.get("/token.json").status_code == 404


def test_health_without_secrets(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["configured"] is False
    assert r.json()["checks"]["openai"] is False
    assert "openai_api_key" not in r.text


def test_documents_public_projection(client, monkeypatch):
    monkeypatch.setattr(main.SyncService, "load_manifest", lambda: {
        "documents": {"drive-123": {"name": "Práctica", "openai_file_id": "file-private", "content_sha256": "private"}},
        "vector_store_id": "vs-private", "pending": {"secret": "private"}})
    data = client.get("/api/documents").json()
    assert data["indexed"] == 1 and data["documents"] == [{"name": "Práctica"}]
    assert "private" not in str(data)


@pytest.mark.parametrize("body", [
    {"question": "a"}, {"question": "   "}, {"question": "x" * 3001},
    {"question": 123}, {"question": "Pregunta", "use_search": "false"},
    {"question": "Pregunta", "history": [{"role": "system", "content": "instrucción"}]},
    {"question": "Pregunta", "history": [{"role": "user", "content": "x"}] * 7},
    {"question": "Pregunta", "extra": "dato"},
])
def test_question_validation(client, body):
    assert client.post("/api/ask", json=body, headers=HEADERS).status_code == 422


def test_missing_key_is_actionable(client):
    response = client.post("/api/ask", json={"question": "Pregunta válida"}, headers=HEADERS)
    assert response.status_code == 400 and "OPENAI_API_KEY" in response.json()["detail"]


@pytest.mark.parametrize("path", ["/", "/api/health", "/api/documents", "/api/ask", "/api/sync"])
def test_service_rejects_non_loopback(client, path):
    with TestClient(main.app, base_url="http://127.0.0.1:5000", client=("192.0.2.5", 4)) as remote:
        assert remote.get(path).status_code == 403


def test_host_origin_and_csrf(client):
    assert client.get("/api/health", headers={"Host": "attacker.example"}).status_code == 403
    assert client.post("/api/sync", json={}, headers=HEADERS | {"Origin": "https://attacker.example"}).status_code == 403
    assert client.post("/api/sync", json={}).status_code == 403
    assert client.post("/api/sync", headers=HEADERS | {"Content-Type": "text/plain"}).status_code == 415
    assert client.get("/api/documents", headers={"Origin": "null"}).status_code == 403


def test_admin_key_enforced(client, monkeypatch):
    monkeypatch.setattr(main, "settings", replace(main.settings, local_admin_key="local-private-test"))
    response = client.post("/api/sync", json={}, headers=HEADERS)
    assert response.status_code == 401
    response = client.post("/api/sync", json={}, headers=HEADERS | {"X-Admin-Key": "local-private-test"})
    assert response.status_code == 400  # supera auth; falta API key


def test_payload_limit(client):
    assert client.post("/api/ask", json={"question": "x" * 40000}, headers=HEADERS).status_code == 413


def test_rate_limit(client, monkeypatch):
    monkeypatch.setattr(main, "settings", replace(main.settings, requests_per_minute=1))
    client.post("/api/ask", json={"question": "Pregunta"}, headers=HEADERS)
    r = client.post("/api/ask", json={"question": "Pregunta"}, headers=HEADERS)
    assert r.status_code == 429 and r.headers["retry-after"] == "60"


def test_busy_query(client):
    main._LOCK.acquire()
    try:
        assert client.post("/api/ask", json={"question": "Pregunta"}, headers=HEADERS).status_code == 409
    finally:
        main._LOCK.release()


def test_unexpected_provider_error_is_redacted(client, monkeypatch):
    monkeypatch.setattr(main, "settings", replace(main.settings, openai_api_key="fake-for-test"))
    service = Mock()
    service.ask.side_effect = RuntimeError("private-provider-payload")
    monkeypatch.setattr(main, "AgentService", lambda: service)
    r = client.post("/api/ask", json={"question": "Pregunta"}, headers=HEADERS)
    assert r.status_code == 502 and "private-provider-payload" not in r.text
    assert not main._LOCK.locked()


def test_ask_contract(client, monkeypatch):
    monkeypatch.setattr(main, "settings", replace(main.settings, openai_api_key="fake-for-test"))
    service = Mock()
    service.ask.return_value = {"success": True, "answer": "No hay evidencia.", "sources": [], "grounded": False}
    monkeypatch.setattr(main, "AgentService", lambda: service)
    r = client.post("/api/ask", json={"question": "  Pregunta válida  "}, headers=HEADERS)
    assert r.status_code == 200 and r.json()["mode"] == "documental"
    assert service.ask.call_args.args[0] == "Pregunta válida"


def test_verified_excerpt_metadata_reaches_browser(client, monkeypatch):
    monkeypatch.setattr(main, "settings", replace(main.settings, openai_api_key="fake-for-test"))
    service = Mock()
    service.ask.return_value = {
        "success": True, "answer": "Se exige control de acceso. [1]", "grounded": True,
        "sources": [{"name": "Plan.pdf", "url": "https://drive.google.com/file/d/drive-test-123/view",
                     "snippet": "La auditoría exige control de acceso.", "evidence_kind": "verified_excerpt",
                     "reference_number": 1, "citation_indices": []}]}
    monkeypatch.setattr(main, "AgentService", lambda: service)
    result = client.post("/api/ask", json={"question": "¿Qué control se exige?"}, headers=HEADERS)
    assert result.status_code == 200
    source = result.json()["sources"][0]
    assert source["evidence_kind"] == "verified_excerpt" and source["reference_number"] == 1
    assert source["citation_indices"] == []
