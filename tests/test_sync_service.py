"""Provider-free tests for incremental state, crash recovery and document validation."""
from copy import deepcopy
from io import BytesIO
import json
from types import SimpleNamespace

import pytest

from app.core.errors import BusyError, DocumentError, ProviderError, StateError
from app.services import sync_service as sync
from app.services.document_service import prepare_document


class Missing(Exception):
    status_code = 404


class Page:
    def __init__(self, data, following=None):
        self.data, self.following = data, following

    def has_next_page(self):
        return self.following is not None

    def get_next_page(self):
        return self.following


class FakeFiles:
    def __init__(self):
        self.records = {}
        self.creates = 0
        self.deletes = []
        self.fail_upload_before = False
        self.fail_upload_after = False
        self.fail_delete = False
        self.page = None

    def create(self, *, file, purpose):
        self.creates += 1
        assert purpose == "assistants"
        if self.fail_upload_before:
            raise TimeoutError("secret-token-and-private-content")
        file_id = f"file-{self.creates}"
        record = SimpleNamespace(id=file_id, filename=file[0], content=file[1])
        self.records[file_id] = record
        if self.fail_upload_after:
            self.fail_upload_after = False
            raise TimeoutError("secret-token-and-private-content")
        return record

    def list(self, **kwargs):
        return self.page or Page(list(self.records.values()))

    def delete(self, file_id):
        self.deletes.append(file_id)
        if self.fail_delete:
            raise TimeoutError("secret-delete")
        if file_id not in self.records:
            raise Missing()
        del self.records[file_id]


class FakeVectorFiles:
    def __init__(self):
        self.records = {}
        self.creates = []
        self.deletes = []
        self.status = "completed"
        self.fail_attach_after = False
        self.fail_attach_before = False
        self.fail_delete = False

    def create(self, *, vector_store_id, file_id, attributes):
        self.creates.append((vector_store_id, file_id, attributes))
        if self.fail_attach_before:
            self.fail_attach_before = False
            raise TimeoutError()
        self.records[file_id] = attributes
        if self.fail_attach_after:
            self.fail_attach_after = False
            raise TimeoutError()
        return SimpleNamespace(id=file_id, status=self.status)

    def retrieve(self, *, vector_store_id, file_id):
        if file_id not in self.records:
            raise Missing()
        return SimpleNamespace(id=file_id, status=self.status)

    def delete(self, *, vector_store_id, file_id):
        self.deletes.append(file_id)
        if self.fail_delete:
            raise TimeoutError("secret-delete")
        if file_id not in self.records:
            raise Missing()
        del self.records[file_id]


class FakeStores:
    def __init__(self):
        self.files = FakeVectorFiles()
        self.metadata = {}
        self.creates = 0
        self.fail_create = False

    def create(self, *, name, metadata):
        self.creates += 1
        if self.fail_create:
            raise TimeoutError("secret-create")
        self.metadata = metadata
        return SimpleNamespace(id="vs-owned", metadata=metadata)

    def retrieve(self, *, vector_store_id):
        return SimpleNamespace(id=vector_store_id, metadata=self.metadata)


class FakeClient:
    def __init__(self):
        self.files = FakeFiles()
        self.vector_stores = FakeStores()
        self.options = []

    def with_options(self, **kwargs):
        self.options.append(kwargs)
        return self


class FakeDrive:
    def __init__(self, items):
        self.items = items
        self.bodies = {item["id"]: b"Contenido documental verificable." for item in items}
        self.downloads = []
        self.list_calls = 0
        self.list_error = None

    def list_files(self, folder_id):
        self.list_calls += 1
        if self.list_error:
            raise self.list_error
        return deepcopy(self.items)

    def get_bytes(self, item):
        self.downloads.append(item["id"])
        body = self.bodies[item["id"]]
        if isinstance(body, Exception):
            raise body
        mime = "text/plain" if item["mimeType"].startswith("application/vnd.google") else item["mimeType"]
        return body, item["name"], mime


def item(drive_id="drive-file-001", name="Práctica.txt", modified="2026-10-15T01:00:00Z", mime="text/plain"):
    return {"id": drive_id, "name": name, "mimeType": mime, "modifiedTime": modified}


@pytest.fixture
def env(monkeypatch, tmp_path):
    settings = SimpleNamespace(drive_folder_id="folder-allowed-001", drive_include_subfolders=False,
                               vector_store_id="", api_timeout_seconds=2.0,
                               index_poll_timeout_seconds=0.03, max_file_bytes=1024 * 1024)
    monkeypatch.setattr(sync, "settings", settings)
    monkeypatch.setattr(sync, "MANIFEST", tmp_path / "data" / "manifest.json")
    client, drive = FakeClient(), FakeDrive([item()])
    return SimpleNamespace(settings=settings, client=client, drive=drive,
                           service=sync.SyncService(client=client, drive=drive))


def test_incremental_add_unchanged_rename_modify_remove(env):
    env.drive.items.append(item("unsupported-file", "data.csv", mime="text/csv"))
    result = env.service.sync()
    assert result["stats"]["added"] == 1
    assert result["stats"]["skipped"] == 1
    assert env.client.vector_stores.files.creates[0][2] == {
        "app_file_id": "file-1", "drive_id": "drive-file-001"}
    assert sync.SyncService.load_manifest()["documents"]["drive-file-001"]["openai_file_id"] == "file-1"
    assert env.service.sync()["stats"]["unchanged"] == 1
    assert env.drive.downloads == ["drive-file-001"]
    env.drive.items[0].update(name="Nombre nuevo.txt", modifiedTime="2")
    renamed = env.service.sync()
    assert renamed["stats"]["renamed"] == 1
    assert renamed["documents"][0]["name"] == "Nombre nuevo.txt"
    assert env.client.files.creates == 1
    env.drive.items[0]["modifiedTime"] = "3"
    env.drive.bodies["drive-file-001"] = b"Contenido cambiado."
    changed = env.service.sync()
    assert changed["stats"]["updated"] == 1
    assert changed["documents"][0]["openai_file_id"] == "file-2"
    assert env.client.files.deletes == ["file-1"]
    assert env.client.vector_stores.files.deletes == ["file-1"]
    env.drive.items = []
    removed = env.service.sync()
    assert removed["stats"]["removed"] == 1 and removed["documents"] == []
    assert not env.client.files.records
    assert not sync.SyncService.load_manifest()["managed_file_ids"]


def test_rename_without_modified_time_change_does_not_download(env):
    env.service.sync()
    env.drive.items[0]["name"] = "Renombrado.txt"
    assert env.service.sync()["stats"]["renamed"] == 1
    assert len(env.drive.downloads) == env.client.files.creates == 1


def test_invalid_documents_fail_partially_and_never_upload(env):
    env.drive.items += [item("empty-file"), item("binary-file")]
    env.drive.bodies.update({"empty-file": b" ", "binary-file": b"data\x00binary"})
    result = env.service.sync()
    assert result["stats"]["added"] == 1
    assert len(result["stats"]["errors"]) == 2
    assert env.client.files.creates == 1


def test_changed_document_download_failure_revokes_old_access(env):
    env.service.sync()
    env.drive.items[0]["modifiedTime"] = "new"
    env.drive.bodies["drive-file-001"] = TimeoutError("secret-token")
    result = env.service.sync()
    assert result["documents"] == []
    assert "secret-token" not in json.dumps(result)
    assert "file-1" in env.client.files.deletes


@pytest.mark.parametrize("failed_body", [TimeoutError("connection failed"), b"invalid\x00binary"])
def test_failed_update_remains_an_update_after_restart(env, failed_body):
    env.service.sync()
    env.drive.items[0]["modifiedTime"] = "new-version"
    env.drive.bodies["drive-file-001"] = failed_body
    failed = env.service.sync()
    assert failed["documents"] == []
    assert failed["pending_count"] == 0
    assert failed["stats"]["added"] == failed["stats"]["updated"] == 0
    state = sync.SyncService.load_manifest()
    assert state["update_intents"] == {"drive-file-001": True}
    assert state["managed_file_ids"] == []  # Obsolete content was fully removed.
    env.drive.bodies["drive-file-001"] = b"Actualizacion recuperada"
    restarted = sync.SyncService(client=env.client, drive=env.drive)
    recovered = restarted.sync()
    assert recovered["stats"]["updated"] == 1
    assert recovered["stats"]["added"] == 0
    assert recovered["documents"][0]["openai_file_id"] == "file-2"
    assert sync.SyncService.load_manifest()["update_intents"] == {}


def test_failed_update_intent_is_cleared_when_source_is_removed(env):
    env.service.sync()
    env.drive.items[0]["modifiedTime"] = "new-version"
    env.drive.bodies["drive-file-001"] = b"invalid\x00binary"
    env.service.sync()
    env.drive.items = []
    assert env.service.sync()["stats"]["removed"] == 1
    assert sync.SyncService.load_manifest()["update_intents"] == {}
    env.drive.items = [item()]
    env.drive.bodies["drive-file-001"] = b"Source reintroduced"
    assert env.service.sync()["stats"]["added"] == 1


def test_upload_lost_response_is_reconciled_without_duplicate(env):
    env.client.files.fail_upload_after = True
    first = env.service.sync()
    assert first["documents"] == [] and first["pending_count"] == 1
    assert "secret-token" not in json.dumps(first)
    assert sync.SyncService.load_manifest()["pending"]["drive-file-001"]["stage"] == "uploading"
    second = env.service.sync()
    assert second["stats"]["added"] == 1
    assert env.client.files.creates == 1
    assert env.client.options[0] == {"max_retries": 0}


def test_ambiguous_missing_upload_requires_manual_reconciliation(env):
    env.client.files.fail_upload_before = True
    env.service.sync()
    second = env.service.sync()
    assert env.client.files.creates == 1
    assert second["pending_count"] == 1
    assert "reconciliación manual" in second["stats"]["errors"][0]["error"]


def test_upload_recovery_traverses_pages(env):
    env.client.files.fail_upload_after = True
    env.service.sync()
    uploaded = next(iter(env.client.files.records.values()))
    env.client.files.page = Page([SimpleNamespace(id="foreign", filename="foreign.txt")], Page([uploaded]))
    assert env.service.sync()["stats"]["added"] == 1
    assert env.client.files.creates == 1


def test_duplicate_recovery_matches_are_rejected(env):
    env.client.files.fail_upload_after = True
    env.service.sync()
    uploaded = next(iter(env.client.files.records.values()))
    env.client.files.page = Page([uploaded, SimpleNamespace(id="duplicate", filename=uploaded.filename)])
    assert env.service.sync()["pending_count"] == 1
    assert not env.client.vector_stores.files.creates


def test_index_timeout_resumes_existing_upload(env, monkeypatch):
    ticks = iter(i * 0.01 for i in range(100))
    monkeypatch.setattr(sync.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(sync.time, "sleep", lambda _: None)
    env.client.vector_stores.files.status = "in_progress"
    first = env.service.sync()
    assert first["documents"] == [] and first["pending_count"] == 1
    assert "sigue en proceso" in first["stats"]["errors"][0]["error"]
    env.client.vector_stores.files.status = "completed"
    second = env.service.sync()
    assert second["stats"]["added"] == 1
    assert env.client.files.creates == 1
    assert len(env.client.vector_stores.files.creates) == 1
    assert all(option["timeout"] <= 0.03 for option in env.client.options if "timeout" in option)


@pytest.mark.parametrize("lose_response", [True, False])
def test_attach_failure_resumes_by_file_id(env, lose_response):
    setattr(env.client.vector_stores.files, "fail_attach_after" if lose_response else "fail_attach_before", True)
    first = env.service.sync()
    assert first["pending_count"] == 1, first
    second = env.service.sync()
    assert second["stats"]["added"] == 1, {
        "first": first, "second": second, "manifest": sync.SyncService.load_manifest()}
    assert env.client.files.creates == 1
    assert len(env.client.vector_stores.files.creates) == (1 if lose_response else 2)


def test_recovered_attach_gets_poll_budget_after_journal_commit(env, monkeypatch):
    env.client.vector_stores.files.fail_attach_before = True
    first = env.service.sync()
    assert first["pending_count"] == 1
    assert sync.SyncService.load_manifest()["pending"]["drive-file-001"]["stage"] == "attaching"
    clock = [0.0]
    original = sync.SyncService.save_manifest

    def slow_journal(data):
        original(data)
        pending = data["pending"].get("drive-file-001")
        if pending and pending["stage"] == "indexing":
            # A successful attach plus its durable local commit can exceed the
            # poll timeout. The following completed retrieval still gets a turn.
            clock[0] += 2 * env.settings.index_poll_timeout_seconds

    monkeypatch.setattr(sync.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(sync.SyncService, "save_manifest", staticmethod(slow_journal))
    second = env.service.sync()
    assert second["stats"]["added"] == 1, {
        "first": first, "second": second, "manifest": sync.SyncService.load_manifest()}
    assert second["pending_count"] == 0
    assert env.client.files.creates == 1
    assert len(env.client.vector_stores.files.creates) == 2


def test_failed_index_retries_same_uploaded_file(env):
    env.client.vector_stores.files.status = "failed"
    assert env.service.sync()["pending_count"] == 1
    env.client.vector_stores.files.status = "completed"
    assert env.service.sync()["stats"]["added"] == 1
    assert env.client.files.creates == 1
    assert env.client.vector_stores.files.deletes == ["file-1"]


def test_changed_pending_source_is_cancelled_and_cleaned(env):
    env.client.vector_stores.files.fail_attach_after = True
    env.service.sync()
    env.drive.items[0]["modifiedTime"] = "new"
    env.drive.bodies["drive-file-001"] = b"Otra version autorizada"
    result = env.service.sync()
    assert result["pending_count"] == 0
    assert result["documents"][0]["openai_file_id"] == "file-2"
    assert env.client.files.deletes == ["file-1"]


def test_deleted_pending_source_is_cleaned(env):
    env.client.vector_stores.files.fail_attach_after = True
    env.service.sync()
    env.drive.items = []
    result = env.service.sync()
    assert result["stats"]["removed"] == 1
    assert result["pending_count"] == 0 and result["documents"] == []
    assert env.client.files.deletes == ["file-1"]


def test_cleanup_failure_keeps_journal_but_revokes_access(env):
    env.service.sync()
    env.drive.items = []
    env.client.files.fail_delete = True
    first = env.service.sync()
    assert first["documents"] == []
    state = sync.SyncService.load_manifest()
    assert state["cleanup"] == {"file-1": {"detached": True}}
    env.client.files.fail_delete = False
    second = env.service.sync()
    assert second["stats"]["errors"] == []
    assert not sync.SyncService.load_manifest()["cleanup"]
    assert env.client.vector_stores.files.deletes == ["file-1"]


def test_empty_scope_does_not_create_remote_store(env):
    env.drive.items = []
    assert env.service.sync()["vector_store_id"] == ""
    assert env.client.vector_stores.creates == 0
    state = sync.SyncService.load_manifest()
    assert state["last_sync"]["stats"]["added"] == 0
    assert state["vector_store_id"] == ""


def test_incomplete_drive_listing_cannot_trigger_deletions(env):
    env.service.sync()
    before = sync.MANIFEST.read_bytes()
    env.drive.list_error = ProviderError("Drive no disponible")
    with pytest.raises(ProviderError):
        env.service.sync()
    assert sync.MANIFEST.read_bytes() == before
    assert env.client.files.deletes == []


@pytest.mark.parametrize("mutation", ["folder", "subfolders", "store"])
def test_scope_and_store_changes_fail_before_side_effects(env, mutation):
    env.service.sync()
    if mutation == "folder":
        env.settings.drive_folder_id = "another-folder-id"
    elif mutation == "subfolders":
        env.settings.drive_include_subfolders = True
    else:
        env.settings.vector_store_id = "vs-different"
    with pytest.raises(StateError, match="reconciliación manual"):
        env.service.sync()
    assert env.drive.list_calls == 1
    assert env.client.files.creates == 1


def test_explicit_foreign_store_is_never_adopted(env):
    env.settings.vector_store_id = "vs-foreign"
    with pytest.raises(StateError, match="alcance"):
        env.service.sync()
    assert env.client.files.creates == env.client.vector_stores.creates == 0


def test_store_creation_failure_is_journaled_and_not_retried(env):
    env.client.vector_stores.fail_create = True
    with pytest.raises(ProviderError):
        env.service.sync()
    with pytest.raises(StateError, match="reconciliación manual"):
        env.service.sync()
    assert env.client.vector_stores.creates == 1


def test_corrupt_or_legacy_manifest_is_never_silently_reset(env):
    sync.MANIFEST.parent.mkdir(parents=True)
    for content in ("{broken", json.dumps({"vector_store_id": "old", "documents": {}})):
        sync.MANIFEST.write_text(content)
        with pytest.raises(StateError):
            env.service.sync()
        assert sync.MANIFEST.read_text() == content
    assert env.client.files.creates == 0


def test_foreign_cleanup_id_fails_validation(env):
    data = sync.SyncService._new_manifest()
    data["vector_store_id"] = "vs-owned"
    data["cleanup"]["file-foreign"] = {"detached": False}
    with pytest.raises(StateError):
        sync.SyncService.save_manifest(data)
    assert not sync.MANIFEST.exists()


def test_atomic_replace_failure_preserves_previous_manifest(env, monkeypatch):
    env.service.sync()
    before = sync.MANIFEST.read_bytes()
    def fail_replace(*_):
        raise OSError("disk unavailable")
    monkeypatch.setattr(sync.os, "replace", fail_replace)
    with pytest.raises(StateError, match="guardar"):
        sync.SyncService.save_manifest(sync.SyncService.load_manifest())
    assert sync.MANIFEST.read_bytes() == before
    assert not list(sync.MANIFEST.parent.glob(".manifest-*.tmp"))


def test_transient_windows_replace_lock_is_retried(env, monkeypatch):
    original = sync.os.replace
    calls = []
    def replace(source, destination):
        calls.append(1)
        if len(calls) <= 2:
            raise PermissionError("sharing violation")
        original(source, destination)
    monkeypatch.setattr(sync.os, "replace", replace)
    sync.SyncService.save_manifest(sync.SyncService._new_manifest())
    assert len(calls) == 3
    assert sync.SyncService.load_manifest()["schema_version"] == sync.SCHEMA_VERSION


def test_crash_after_upload_before_manifest_commit_recovers(env, monkeypatch):
    original = sync.SyncService.save_manifest
    fail = [True]
    def save(data):
        pending = data["pending"].get("drive-file-001")
        if pending and pending["stage"] == "uploaded" and fail[0]:
            fail[0] = False
            raise sync._ManifestWriteError("No se pudo guardar")
        original(data)
    monkeypatch.setattr(sync.SyncService, "save_manifest", staticmethod(save))
    with pytest.raises(StateError):
        env.service.sync()
    assert sync.SyncService.load_manifest()["pending"]["drive-file-001"]["stage"] == "uploading"
    assert env.service.sync()["stats"]["added"] == 1
    assert env.client.files.creates == 1


def test_shared_lock_blocks_sync_and_is_released_after_failure(env):
    sync._LOCK.acquire()
    try:
        with pytest.raises(BusyError):
            env.service.sync()
    finally:
        sync._LOCK.release()
    env.drive.list_error = ProviderError("failed")
    with pytest.raises(ProviderError):
        env.service.sync()
    assert sync._LOCK.acquire(blocking=False)
    sync._LOCK.release()


def test_last_sync_records_verifiable_partial_statistics(env):
    env.client.files.fail_upload_after = True
    result = env.service.sync()
    state = sync.SyncService.load_manifest()
    assert state["last_sync"]["stats"] == result["stats"]
    assert state["last_sync"]["pending_count"] == result["pending_count"] == 1
    assert state["last_sync"]["cleanup_count"] == 0
    assert state["last_sync"]["at"].endswith("+00:00")


def test_same_checksum_avoids_download_after_metadata_change(env):
    env.drive.items[0]["md5Checksum"] = "content-checksum"
    env.service.sync()
    env.drive.items[0].update(modifiedTime="new-time", version="2", name="Renombrado.txt")
    result = env.service.sync()
    assert result["stats"]["renamed"] == 1
    assert len(env.drive.downloads) == env.client.files.creates == 1


def test_changed_checksum_with_same_timestamp_cannot_serve_stale_content(env):
    env.drive.items[0]["md5Checksum"] = "old-checksum"
    env.service.sync()
    env.drive.items[0]["md5Checksum"] = "new-checksum"
    env.drive.bodies["drive-file-001"] = b"New evidence"
    assert env.service.sync()["stats"]["updated"] == 1
    assert env.client.files.creates == 2


def test_missing_modified_time_does_not_imply_unchanged_content(env):
    env.drive.items[0].pop("modifiedTime")
    env.service.sync()
    env.drive.bodies["drive-file-001"] = b"New evidence"
    assert env.service.sync()["stats"]["updated"] == 1


def test_recovery_pagination_is_bounded(env, monkeypatch):
    env.client.files.fail_upload_after = True
    env.service.sync()
    endless_page = Page([])
    endless_page.following = endless_page
    env.client.files.page = endless_page
    monkeypatch.setattr(sync, "_MAX_RECOVERY_PAGES", 2)
    result = env.service.sync()
    assert "límite de páginas" in result["stats"]["errors"][0]["error"]
    assert env.client.files.creates == 1


def test_unchanged_documents_write_only_final_summary(env, monkeypatch):
    env.service.sync()
    original = sync.SyncService.save_manifest
    writes = []
    def save(data):
        writes.append(1)
        original(data)
    monkeypatch.setattr(sync.SyncService, "save_manifest", staticmethod(save))
    env.service.sync()
    assert len(writes) == 1


@pytest.mark.parametrize("key", ["openai_file_id", "mime_type"])
def test_invalid_nested_document_types_fail_as_controlled_state_errors(env, key):
    env.service.sync()
    state = sync.SyncService.load_manifest()
    state["documents"]["drive-file-001"][key] = []
    sync.MANIFEST.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(StateError):
        sync.SyncService.load_manifest()


@pytest.mark.parametrize("key,value", [("stage", []), ("openai_file_id", []), ("content_sha256", "z" * 64)])
def test_malformed_pending_state_is_rejected_without_remote_effects(env, key, value):
    env.client.files.fail_upload_after = True
    env.service.sync()
    state = sync.SyncService.load_manifest()
    state["pending"]["drive-file-001"][key] = value
    sync.MANIFEST.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(StateError):
        env.service.sync()
    assert env.client.files.creates == 1
    assert not env.client.vector_stores.files.creates


@pytest.mark.parametrize("raw", [b"", b"  \n", b"\xffbad-encoding", b"binary\x00data"])
def test_text_validation_rejects_unusable_content(raw):
    with pytest.raises(DocumentError):
        prepare_document(raw, "document.txt", "text/plain", 1000)


def test_utf16_and_utf8_bom_are_normalized_and_names_sanitized():
    text = "Información documentada\r\npara consultar."
    prepared = prepare_document(text.encode("utf-16"), "../../practica.exe", "text/plain", 1000)
    assert prepared.content == "Información documentada\npara consultar.".encode()
    assert prepared.filename == "practica.txt"
    assert prepare_document(b"\xef\xbb\xbfValid text", "a.txt", "text/plain", 1000).content == b"Valid text"


def test_size_limit_is_checked_before_processing():
    with pytest.raises(DocumentError, match="tamaño"):
        prepare_document(b"abc", "a.txt", "text/plain", 2)


def test_scanned_and_corrupt_pdfs_are_rejected():
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    output = BytesIO()
    writer.write(output)
    with pytest.raises(DocumentError, match="OCR"):
        prepare_document(output.getvalue(), "scan.pdf", "application/pdf", 100000)
    with pytest.raises(DocumentError, match="dañado"):
        prepare_document(b"not a PDF", "bad.pdf", "application/pdf", 100000)


def test_pdf_with_text_preserves_original_pdf_bytes():
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 20 100 Td (Documented evidence) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    raw = output.getvalue()
    prepared = prepare_document(raw, "wrong-extension.txt", "application/pdf", 100000)
    assert prepared.content == raw and prepared.filename.endswith(".pdf")
    assert prepared.text_characters > 0
