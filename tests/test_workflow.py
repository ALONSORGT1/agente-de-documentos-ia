"""Local HTTP workflow through the real services and official OpenAI SDK.

Only the provider's HTTP transport and Drive are simulated. No credentials,
network calls, OAuth flow or production manifest are used by these tests.
"""
from email import policy
from email.parser import BytesParser
import json
import re
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient
from openai import OpenAI
import pytest

import app.core.clients as client_module
import app.core.config as config_module
from app.core.config import Settings
import app.main as main
import app.services.agent_service as agent_module
import app.services.sync_service as sync_module


HEADERS = {
    "X-Requested-With": "AgenteDocumentalTIC",
    "Content-Type": "application/json",
    "Origin": "http://127.0.0.1:5000",
}


class ExampleDrive:
    def __init__(self, folder):
        self.folder = folder
        self.files = {
            f"drive-document-{number}": {
                "id": f"drive-document-{number}",
                "name": f"documento_{number}.txt",
                "mimeType": "text/plain",
                "modifiedTime": "2026-10-15T12:00:00Z",
                "version": "1",
                "webViewLink": f"https://drive.google.com/file/d/drive-document-{number}/view",
            }
            for number in range(1, 6)
        }
        self.downloads = []

    def list_files(self, folder):
        assert folder == self.folder
        return [dict(item) for item in self.files.values()]

    def get_bytes(self, item):
        assert item["id"] in self.files
        self.downloads.append(item["id"])
        content = f"Documento {item['id']}: la conexión académica utiliza TLS y acceso autorizado.\n"
        return content.encode("utf-8"), item["name"], "text/plain"


class OpenAIHTTPFixture:
    """Faithful JSON/multipart endpoint shapes parsed by openai 2.x itself."""

    def __init__(self):
        self.metadata = None
        self.uploads = {}
        self.attachments = {}
        self.deleted = []
        self.detached = []
        self.response_requests = []
        self.reply = "current"

    def vector_store(self):
        return {
            "id": "vs-workflow", "object": "vector_store", "created_at": 1,
            "name": "Archivo TIC IA - Documentos", "status": "completed",
            "usage_bytes": 1234, "last_active_at": 1, "metadata": self.metadata,
            "file_counts": {"in_progress": 0, "completed": len(self.attachments),
                            "failed": 0, "cancelled": 0, "total": len(self.attachments)},
        }

    def vector_file(self, file_id):
        return {
            "id": file_id, "object": "vector_store.file", "created_at": 1,
            "vector_store_id": "vs-workflow", "status": "completed",
            "usage_bytes": 123, "last_error": None,
            "attributes": self.attachments[file_id],
        }

    def result(self, file_id):
        return {
            "file_id": file_id, "filename": self.uploads[file_id]["filename"],
            "attributes": {"app_file_id": file_id}, "score": 0.96,
            "text": self.uploads[file_id]["text"],
        }

    def response(self, payload):
        assert payload["store"] is False
        assert payload["model"] == "gpt-4.1-mini"
        assert payload["input"][-1]["role"] == "user"
        assert all(item["role"] == "user" for item in payload["input"])
        self.response_requests.append(payload)
        usage = {"input_tokens": 100, "output_tokens": 30, "total_tokens": 130}
        text_format = payload.get("text", {}).get("format", {})
        if text_format.get("type") == "json_schema":
            assert self.reply == "no_native_citations"
            assert len(self.response_requests) == 2
            assert not any(key in payload for key in ("tools", "tool_choice", "include", "max_tool_calls"))
            assert text_format["name"] == "verified_document_excerpt"
            assert text_format["strict"] is True
            schema = text_format["schema"]
            assert schema["type"] == "object" and schema["additionalProperties"] is False
            assert set(schema["required"]) == {"answerable", "claims"}
            assert set(schema["properties"]) == {"answerable", "claims"}
            evidence_schema = schema["properties"]["claims"]["items"]["properties"]["evidence"]["items"]
            assert evidence_schema["additionalProperties"] is False
            assert set(evidence_schema["required"]) == {"ref"}
            assert set(evidence_schema["properties"]) == {"ref"}
            assert len(payload["input"]) == 1
            context = json.loads(payload["input"][0]["content"])
            assert context["question"] == "Compara la conexión académica de los dos primeros documentos."
            assert context["previous_questions"] == ["¿Qué requisitos de conexión están documentados?"]
            fragments = context["fragments"]
            active = sorted(self.attachments)[:2]
            assert [fragment["ref"] for fragment in fragments] == ["R1", "R2"]
            assert evidence_schema["properties"]["ref"]["enum"] == ["R1", "R2"]
            assert [fragment["text"].strip() for fragment in fragments] == [
                self.uploads[file_id]["text"].strip() for file_id in active]
            assert [fragment["name"] for fragment in fragments] == ["documento_1.txt", "documento_2.txt"]
            claims = [{
                "text": f"El documento {number} indica que la conexión académica utiliza TLS y acceso autorizado.",
                "evidence": [{"ref": fragment["ref"]}],
            } for number, fragment in enumerate(fragments, 1)]
            results, cited = [], []
            answer = json.dumps({"answerable": True, "claims": claims}, ensure_ascii=False)
            usage = {"input_tokens": 70, "output_tokens": 40, "total_tokens": 110}
        elif "tools" not in payload:
            assert "tool_choice" not in payload and "include" not in payload
            results, cited, answer = [], [], "Respuesta experimental, sin consultar documentos."
        else:
            assert payload["tools"] == [{
                "type": "file_search", "vector_store_ids": ["vs-workflow"],
                "max_num_results": 12,
                "filters": {"type": "in", "key": "app_file_id",
                            "value": sorted(self.attachments)},
            }]
            assert payload["tool_choice"] == "required"
            assert payload["include"] == ["file_search_call.results"]
            active = sorted(self.attachments)[:2]
            results, cited = list(active), list(active)
            answer = "Los documentos indican usar TLS y acceso autorizado."
            if self.reply == "no_native_citations":
                cited = []
                answer = "BORRADOR SIN CITAS QUE DEBE DESCARTARSE."
            elif self.reply != "current":
                assert self.deleted
                stale = self.deleted[0]
                if self.reply in {"stale_result", "stale_both"}:
                    results.append(stale)
                if self.reply in {"stale_citation", "stale_both"}:
                    cited.append(stale)
                answer = "AFIRMACIÓN OBSOLETA QUE DEBE DESCARTARSE."
        output = []
        if "tools" in payload:
            output.append({
                "id": "fs-workflow", "type": "file_search_call", "status": "completed",
                "queries": [payload["input"][-1]["content"]],
                "results": [self.result(file_id) for file_id in results],
            })
        output.append({
            "id": "msg-workflow", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": answer,
                         "annotations": [{"type": "file_citation", "file_id": file_id,
                                          "filename": self.uploads[file_id]["filename"], "index": index}
                                         for index, file_id in enumerate(cited, 10)]}],
        })
        return {
            "id": "resp-workflow", "object": "response", "created_at": 1,
            "status": "completed", "error": None, "incomplete_details": None,
            "model": payload["model"], "instructions": payload["instructions"],
            "output": output, "parallel_tool_calls": True, "temperature": 1.0,
            "top_p": 1.0, "tool_choice": payload.get("tool_choice", "auto"),
            "tools": payload.get("tools", []), "metadata": {},
            "usage": {**usage,
                      "input_tokens_details": {"cached_tokens": 0},
                      "output_tokens_details": {"reasoning_tokens": 0}},
        }

    def __call__(self, request):
        assert request.url.host == "openai.invalid"
        assert request.headers["authorization"] == "Bearer fake-key-for-local-test"
        path, method = request.url.path, request.method
        if path == "/v1/vector_stores" and method == "POST":
            payload = json.loads(request.content)
            self.metadata = payload["metadata"]
            assert self.metadata == {"app_owner": sync_module.APP_OWNER,
                                     "app_scope": sync_module.scope_fingerprint()}
            return httpx.Response(200, json=self.vector_store())
        if path == "/v1/vector_stores/vs-workflow" and method == "GET":
            return httpx.Response(200, json=self.vector_store())
        if path == "/v1/files" and method == "POST":
            # Parse the real SDK's multipart encoding, including its filename.
            mime = BytesParser(policy=policy.default).parsebytes(
                b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content)
            fields = {part.get_param("name", header="content-disposition"): part for part in mime.iter_parts()}
            assert fields["purpose"].get_payload(decode=True) == b"assistants"
            filename = fields["file"].get_filename()
            assert re.fullmatch(r"tic_[0-9a-f]{32}_documento_[1-5]\.txt", filename)
            content = fields["file"].get_payload(decode=True)
            assert b"TLS" in content and content.decode("utf-8").startswith("Documento drive-document-")
            file_id = f"file-workflow-{len(self.uploads) + 1}"
            self.uploads[file_id] = {"filename": filename, "text": content.decode("utf-8")}
            return httpx.Response(200, json={
                "id": file_id, "object": "file", "bytes": len(content), "created_at": 1,
                "filename": filename, "purpose": "assistants", "status": "processed",
            })
        if path == "/v1/vector_stores/vs-workflow/files" and method == "POST":
            payload = json.loads(request.content)
            file_id = payload["file_id"]
            assert file_id in self.uploads and file_id not in self.attachments
            assert payload["attributes"]["app_file_id"] == file_id
            assert payload["attributes"]["drive_id"] in self.uploads[file_id]["text"]
            self.attachments[file_id] = payload["attributes"]
            return httpx.Response(200, json=self.vector_file(file_id))
        if path.startswith("/v1/vector_stores/vs-workflow/files/"):
            file_id = path.rsplit("/", 1)[-1]
            if method == "GET":
                return httpx.Response(200, json=self.vector_file(file_id))
            if method == "DELETE":
                assert file_id in self.attachments
                self.attachments.pop(file_id)
                self.detached.append(file_id)
                return httpx.Response(200, json={"id": file_id, "object": "vector_store.file.deleted", "deleted": True})
        if path.startswith("/v1/files/") and method == "DELETE":
            file_id = path.rsplit("/", 1)[-1]
            assert file_id in self.detached
            self.deleted.append(file_id)
            return httpx.Response(200, json={"id": file_id, "object": "file", "deleted": True})
        if path == "/v1/responses" and method == "POST":
            return httpx.Response(200, json=self.response(json.loads(request.content)))
        raise AssertionError(f"Unexpected provider request: {method} {path}")


@pytest.fixture
def workflow(monkeypatch, tmp_path):
    config = Settings(
        openai_api_key="fake-key-for-local-test", openai_model="gpt-4.1-mini",
        drive_folder_id="authorized-folder-workflow", vector_store_id="", local_admin_key="",
        drive_include_subfolders=False, requests_per_minute=120, search_max_results=12,
        google_credentials_path=tmp_path / "unused-credentials.json",
        google_token_path=tmp_path / "unused-token.json",
    )
    for module in (main, sync_module, agent_module, config_module, client_module):
        monkeypatch.setattr(module, "settings", config)
    manifest = tmp_path / "manifest.json"
    monkeypatch.setattr(sync_module, "MANIFEST", manifest)
    drive = ExampleDrive(config.drive_folder_id)
    provider = OpenAIHTTPFixture()
    with httpx.Client(transport=httpx.MockTransport(provider)) as transport:
        sdk = OpenAI(api_key=config.openai_api_key, base_url="https://openai.invalid/v1",
                     max_retries=0, http_client=transport)
        monkeypatch.setattr(sync_module, "get_openai_client", lambda: sdk)
        monkeypatch.setattr(agent_module, "get_openai_client", lambda: sdk)
        monkeypatch.setattr(sync_module, "DriveService", lambda: drive)
        main._requests.clear()
        with TestClient(main.app, base_url="http://127.0.0.1:5000", client=("127.0.0.1", 45001)) as client:
            yield SimpleNamespace(client=client, provider=provider, drive=drive, manifest=manifest)
    assert not sync_module._LOCK.locked()


def test_local_workflow_sync_query_deletion_and_stale_evidence(workflow):
    client, provider, drive = workflow.client, workflow.provider, workflow.drive
    first = client.post("/api/sync", json={}, headers=HEADERS)
    assert first.status_code == 200, first.text
    assert first.json()["stats"]["added"] == 5
    assert first.json()["stats"]["errors"] == []
    assert len(provider.uploads) == len(provider.attachments) == len(drive.downloads) == 5
    assert workflow.manifest.is_file()

    documents = client.get("/api/documents")
    assert documents.status_code == 200
    assert documents.json()["indexed"] == 5
    assert len(documents.json()["documents"]) == 5
    assert documents.json()["last_sync"]["stats"]["added"] == 5
    assert "openai_file_id" not in documents.text and "content_sha256" not in documents.text

    second = client.post("/api/sync", json={}, headers=HEADERS)
    assert second.status_code == 200, second.text
    assert second.json()["stats"]["unchanged"] == 5
    assert second.json()["stats"]["added"] == 0
    assert len(provider.uploads) == len(drive.downloads) == 5

    answer = client.post("/api/ask", json={"question": "¿Cómo se establece la conexión académica?"}, headers=HEADERS)
    assert answer.status_code == 200, answer.text
    payload = answer.json()
    assert payload["grounded"] is True and payload["mode"] == "documental"
    assert len(payload["sources"]) == 2
    assert payload["usage"]["total_tokens"] == 130
    for source in payload["sources"]:
        assert source["file_id"] in provider.attachments
        assert source["snippet"] == provider.uploads[source["file_id"]]["text"]
        assert source["name"] == drive.files[source["drive_id"]]["name"]
        assert source["citation_indices"] and isinstance(source["citation_indices"][0], int)
        assert source["url"] == f"https://drive.google.com/file/d/{source['drive_id']}/view"

    removed_drive_id = "drive-document-1"
    removed_file_id = next(file_id for file_id, attrs in provider.attachments.items() if attrs["drive_id"] == removed_drive_id)
    del drive.files[removed_drive_id]
    third = client.post("/api/sync", json={}, headers=HEADERS)
    assert third.status_code == 200, third.text
    assert third.json()["stats"]["removed"] == 1
    assert third.json()["stats"]["unchanged"] == 4
    assert third.json()["stats"]["errors"] == []
    assert removed_file_id in provider.detached and removed_file_id in provider.deleted
    current = client.get("/api/documents").json()
    assert current["indexed"] == 4
    assert removed_drive_id not in {doc["drive_id"] for doc in current["documents"]}
    assert len(provider.uploads) == 5  # deletion and unchanged sync do not re-upload.

    followup = client.post("/api/ask", json={
        "question": "¿Qué dicen los documentos que siguen autorizados?",
        "history": [{"role": "user", "content": "Pregunta previa"},
                    {"role": "assistant", "content": "Afirmación previa sin evidencia"}],
    }, headers=HEADERS)
    assert followup.status_code == 200, followup.text
    assert followup.json()["grounded"] is True
    assert all(source["drive_id"] != removed_drive_id for source in followup.json()["sources"])
    assert provider.response_requests[-1]["input"][0]["content"] == "Pregunta previa"
    assert removed_file_id not in provider.response_requests[-1]["tools"][0]["filters"]["value"]

    # Simulate provider eventual consistency despite the current authorization filter.
    for mode in ("stale_result", "stale_citation", "stale_both"):
        provider.reply = mode
        stale = client.post("/api/ask", json={"question": "¿Qué evidencia sigue vigente?"}, headers=HEADERS)
        assert stale.status_code == 200, stale.text
        data = stale.json()
        assert data["grounded"] is False and data["sources"] == []
        assert data["answer"] == agent_module.INSUFFICIENT
        assert "AFIRMACIÓN OBSOLETA" not in data["answer"]


def test_experimental_workflow_uses_no_search_tools_or_document_manifest(workflow):
    response = workflow.client.post("/api/ask", json={
        "question": "Explica TLS en modo experimental.", "use_search": False,
        "history": [{"role": "assistant", "content": "No debe convertirse en evidencia."}],
    }, headers=HEADERS)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["mode"] == "experimental" and data["grounded"] is False
    assert data["sources"] == [] and "sin File Search" in data["warning"]
    assert workflow.provider.response_requests[0]["input"] == [
        {"role": "user", "content": "Explica TLS en modo experimental."}]
    assert not workflow.manifest.exists()
    assert workflow.provider.uploads == {} and workflow.drive.downloads == []


def test_documental_workflow_verifies_excerpt_fallback_with_real_sdk(workflow):
    synchronized = workflow.client.post("/api/sync", json={}, headers=HEADERS)
    assert synchronized.status_code == 200, synchronized.text
    assert synchronized.json()["stats"]["added"] == 5
    workflow.provider.reply = "no_native_citations"
    response = workflow.client.post("/api/ask", json={
        "question": "Compara la conexión académica de los dos primeros documentos.",
        "history": [{"role": "user", "content": "¿Qué requisitos de conexión están documentados?"},
                    {"role": "assistant", "content": "Afirmación previa que no aporta evidencia."}],
    }, headers=HEADERS)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["grounded"] is True and data["mode"] == "documental"
    assert data["usage"] == {"input_tokens": 170, "output_tokens": 70, "total_tokens": 240}
    assert "BORRADOR SIN CITAS" not in data["answer"]
    assert "[1]" in data["answer"] and "[2]" in data["answer"]
    assert len(data["sources"]) == 2
    assert [source["reference_number"] for source in data["sources"]] == [1, 2]
    for number, source in enumerate(data["sources"], 1):
        assert source["evidence_kind"] == "verified_excerpt"
        assert source["citation_indices"] == []
        assert source["file_id"] == f"file-workflow-{number}"
        assert source["drive_id"] == f"drive-document-{number}"
        assert source["name"] == f"documento_{number}.txt"
        assert source["url"] == f"https://drive.google.com/file/d/drive-document-{number}/view"
        assert source["snippet"] == workflow.provider.uploads[source["file_id"]]["text"].strip()
    primary, fallback = workflow.provider.response_requests
    assert "tools" in primary and primary["include"] == ["file_search_call.results"]
    assert fallback["text"]["format"]["type"] == "json_schema"
    assert fallback["text"]["format"]["strict"] is True
    assert not any(key in fallback for key in ("tools", "tool_choice", "include", "max_tool_calls"))
