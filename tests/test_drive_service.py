"""Drive simulado: estas pruebas nunca autorizan ni consultan APIs reales."""
from collections import deque
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core.errors import ConfigurationError, DocumentError, ProviderError
from app.services import drive_service as module
from app.services.drive_service import DOC_MIME, FOLDER_MIME, SCOPES, DriveService

ROOT = "authorized-folder-123"
CHILD = "sub-folder-12345"
FILE = "document-file-123"
OUTSIDE = "outside-folder-123"


def metadata(file_id=FILE, *, mime="text/plain", parent=ROOT, **extra):
    return {"id": file_id, "name": "Apuntes.txt", "mimeType": mime,
            "modifiedTime": "2026-10-15T10:00:00Z", "version": "1",
            "parents": [parent] if parent else [], "trashed": False, **extra}


def config(tmp_path, **overrides):
    values = dict(drive_folder_id=ROOT, drive_include_subfolders=False,
                  api_timeout_seconds=12, oauth_timeout_seconds=30,
                  max_file_bytes=1024, drive_max_files=20,
                  google_credentials_path=tmp_path / "credentials.json",
                  google_token_path=tmp_path / "token.json")
    return SimpleNamespace(**(values | overrides))


class Request:
    def __init__(self, result):
        self.result = result
        self.retries = []

    def execute(self, *, num_retries):
        self.retries.append(num_retries)
        if isinstance(self.result, Exception):
            raise self.result
        return deepcopy(self.result)


class Files:
    def __init__(self, pages=(), items=None, data=b"contenido documental"):
        self.pages = deque(pages)
        self.items = {ROOT: metadata(ROOT, mime=FOLDER_MIME, parent=None),
                      FILE: metadata(), **(items or {})}
        self.data = data
        self.list_calls = []
        self.get_calls = []
        self.media_calls = []
        self.export_calls = []
        self.requests = []

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        value = self.items[kwargs["fileId"]]
        if isinstance(value, deque):
            value = value.popleft()
        request = Request(value)
        self.requests.append(request)
        return request

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        request = Request(self.pages.popleft())
        self.requests.append(request)
        return request

    def get_media(self, **kwargs):
        self.media_calls.append(kwargs)
        return SimpleNamespace(data=self.data)

    def export_media(self, **kwargs):
        self.export_calls.append(kwargs)
        return SimpleNamespace(data=self.data)


@pytest.fixture(autouse=True)
def no_real_clients(monkeypatch):
    # Toda vía no simulada falla inmediatamente, sin abrir navegador ni red.
    monkeypatch.setattr(module, "build", Mock(side_effect=AssertionError("Cliente real prohibido en test")))
    monkeypatch.setattr(module.InstalledAppFlow, "from_client_config",
                        Mock(side_effect=AssertionError("OAuth real prohibido en test")))


@pytest.fixture
def downloads(monkeypatch):
    calls = []

    class Download:
        def __init__(self, stream, request, *, chunksize):
            self.stream, self.request = stream, request
            calls.append({"chunksize": chunksize})

        def next_chunk(self, *, num_retries):
            calls[-1]["retries"] = num_retries
            self.stream.write(self.request.data)
            return None, True

    monkeypatch.setattr(module, "MediaIoBaseDownload", Download)
    return calls


def drive(tmp_path, files, **overrides):
    return DriveService(service=SimpleNamespace(files=lambda: files), config=config(tmp_path, **overrides))


@pytest.mark.parametrize("folder", ["", "short", "https://drive.google.com/folders/abc", "abc' or trashed=false", "a" * 201])
def test_invalid_folder_rejected_before_oauth_or_api(tmp_path, monkeypatch, folder):
    service = DriveService(config=config(tmp_path))
    authorize = Mock()
    monkeypatch.setattr(service, "_credentials", authorize)
    with pytest.raises(ConfigurationError):
        service.list_files(folder)
    authorize.assert_not_called()


def test_different_configured_folder_rejected_without_access(tmp_path):
    files = Files()
    with pytest.raises(ConfigurationError):
        drive(tmp_path, files).list_files(OUTSIDE)
    assert files.get_calls == []


@pytest.mark.parametrize("folder", [metadata(ROOT, mime="text/plain", parent=None),
                                    metadata(ROOT, mime=FOLDER_MIME, parent=None, trashed=True)])
def test_folder_must_exist_be_folder_and_not_trashed(tmp_path, folder):
    files = Files(items={ROOT: folder})
    with pytest.raises(ConfigurationError):
        drive(tmp_path, files).list_files(ROOT)
    assert files.list_calls == []


def test_folder_permission_error_is_sanitized(tmp_path):
    files = Files(items={ROOT: RuntimeError("access_token=private-value")})
    with pytest.raises(ProviderError) as error:
        drive(tmp_path, files).list_files(ROOT)
    assert "private-value" not in str(error.value)


def test_list_paginates_and_requests_change_and_parent_metadata(tmp_path):
    first = metadata()
    other = metadata("document-file-456", mime="image/png")
    files = Files([{"files": [first], "nextPageToken": "page-two"}, {"files": [other]}])
    assert drive(tmp_path, files).list_files(ROOT) == [first, other]
    assert [call["pageToken"] for call in files.list_calls] == [None, "page-two"]
    for call in files.list_calls:
        assert call["q"] == f"'{ROOT}' in parents and trashed = false"
        assert all(field in call["fields"] for field in ("md5Checksum", "version", "parents", "incompleteSearch"))
    assert all(request.retries == [module.RETRIES] for request in files.requests)


def test_empty_folder_is_valid(tmp_path):
    assert drive(tmp_path, Files([{}])).list_files(ROOT) == []


@pytest.mark.parametrize("pages", [
    [{"files": [metadata()], "incompleteSearch": True}],
    [{"files": None}],
    [{"files": [], "nextPageToken": "same"}, {"files": [], "nextPageToken": "same"}],
    [{"files": [metadata(), metadata()]}],
    [{"files": [metadata(parent=OUTSIDE)]}],
    [{"files": [{"id": FILE}]}],
    [{"files": [], "nextPageToken": 0}],
])
def test_incomplete_or_inconsistent_listing_is_never_returned(tmp_path, pages):
    with pytest.raises(ProviderError):
        drive(tmp_path, Files(pages)).list_files(ROOT)


def test_file_limit_aborts_instead_of_truncating(tmp_path):
    files = Files([{"files": [metadata(), metadata("document-file-456")]}])
    with pytest.raises(ProviderError, match="DRIVE_MAX_FILES"):
        drive(tmp_path, files, drive_max_files=1).list_files(ROOT)


def test_endless_empty_pages_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "MAX_DEPTH", 0)
    files = Files([{"files": [], "nextPageToken": str(index)} for index in range(5)])
    with pytest.raises(ProviderError, match="límite seguro"):
        drive(tmp_path, files, drive_max_files=1).list_files(ROOT)
    assert len(files.list_calls) == 2


def test_default_skips_subfolders_and_shortcuts(tmp_path):
    child = metadata(CHILD, mime=FOLDER_MIME)
    shortcut = metadata("shortcut-file-123", mime=module.SHORTCUT_MIME)
    files = Files([{"files": [child, shortcut, metadata()]}])
    assert drive(tmp_path, files).list_files(ROOT) == [metadata()]
    assert len(files.list_calls) == 1


def test_subfolders_are_explicit_and_traversed(tmp_path):
    child = metadata(CHILD, mime=FOLDER_MIME)
    nested = metadata(parent=CHILD)
    files = Files([{"files": [child]}, {"files": [nested]}], items={CHILD: child})
    assert drive(tmp_path, files, drive_include_subfolders=True).list_files(ROOT) == [nested]
    assert files.list_calls[1]["q"].startswith(f"'{CHILD}'")


def test_nested_folder_moved_during_listing_aborts(tmp_path):
    child = metadata(CHILD, mime=FOLDER_MIME)
    files = Files([{"files": [child]}], items={CHILD: metadata(CHILD, mime=FOLDER_MIME, parent=OUTSIDE),
                                             OUTSIDE: metadata(OUTSIDE, mime=FOLDER_MIME, parent=None)})
    with pytest.raises((DocumentError, ProviderError)):
        drive(tmp_path, files, drive_include_subfolders=True).list_files(ROOT)
    assert len(files.list_calls) == 1


def test_nested_depth_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "MAX_DEPTH", 0)
    files = Files([{"files": [metadata(CHILD, mime=FOLDER_MIME)]}])
    with pytest.raises(ProviderError, match="límite"):
        drive(tmp_path, files, drive_include_subfolders=True).list_files(ROOT)


@pytest.mark.parametrize("mime", ["text/plain", "application/pdf", DOC_MIME])
def test_downloads_priority_formats_with_bounded_retries(tmp_path, downloads, mime):
    item = metadata(mime=mime)
    files = Files(items={FILE: item})
    raw, name, target = drive(tmp_path, files).get_bytes(item)
    assert raw == files.data
    assert target == ("text/plain" if mime == DOC_MIME else mime)
    assert name == item["name"] + (".txt" if mime == DOC_MIME else "")
    assert bool(files.export_calls) == (mime == DOC_MIME)
    assert downloads[0]["retries"] == module.RETRIES
    assert [call["fileId"] for call in files.get_calls].count(FILE) == 2


@pytest.mark.parametrize("mime", ["image/png", module.SHORTCUT_MIME, FOLDER_MIME])
def test_no_download_of_unsupported_files_or_shortcuts(tmp_path, mime):
    files = Files(items={FILE: metadata(mime=mime)})
    with pytest.raises(DocumentError):
        drive(tmp_path, files).get_bytes({"id": FILE})
    assert files.media_calls == files.export_calls == []


@pytest.mark.parametrize("updates", [{"parent": OUTSIDE}, {"trashed": True}, {"version": "2"}])
def test_changed_or_moved_files_are_not_downloaded(tmp_path, updates):
    files = Files(items={FILE: metadata(**updates)})
    with pytest.raises(DocumentError):
        drive(tmp_path, files).get_bytes(metadata())
    assert files.media_calls == []


def test_movement_during_download_discards_bytes(tmp_path, downloads):
    files = Files(items={FILE: deque([metadata(), metadata(parent=OUTSIDE)])})
    with pytest.raises(DocumentError):
        drive(tmp_path, files).get_bytes(metadata())
    assert len(files.media_calls) == 1


def test_subfolder_scope_rechecked_to_root(tmp_path, downloads):
    child = metadata(CHILD, mime=FOLDER_MIME)
    files = Files(items={CHILD: child, FILE: metadata(parent=CHILD)})
    assert drive(tmp_path, files, drive_include_subfolders=True).get_bytes(metadata(parent=CHILD))[0] == files.data
    files.items[CHILD] = metadata(CHILD, mime=FOLDER_MIME, parent=OUTSIDE)
    files.items[OUTSIDE] = metadata(OUTSIDE, mime=FOLDER_MIME, parent=None)
    with pytest.raises(DocumentError):
        drive(tmp_path, files, drive_include_subfolders=True).get_bytes(metadata(parent=CHILD))


def test_nested_file_not_allowed_by_default(tmp_path):
    files = Files(items={FILE: metadata(parent=CHILD)})
    with pytest.raises(DocumentError):
        drive(tmp_path, files).get_bytes(metadata(parent=CHILD))
    assert files.media_calls == []


@pytest.mark.parametrize("size", ["1025", "-1", "not-a-size"])
def test_size_metadata_is_enforced_before_download(tmp_path, size):
    files = Files(items={FILE: metadata(size=size)})
    with pytest.raises(DocumentError):
        drive(tmp_path, files).get_bytes(metadata())
    assert files.media_calls == []


def test_actual_download_size_is_enforced_for_exports(tmp_path, downloads):
    files = Files(items={FILE: metadata(mime=DOC_MIME)}, data=b"x" * 1025)
    with pytest.raises(DocumentError, match="tamaño"):
        drive(tmp_path, files).get_bytes(metadata(mime=DOC_MIME))


def test_network_download_failure_does_not_expose_provider_details(tmp_path, monkeypatch):
    download = Mock()
    download.next_chunk.side_effect = OSError("Bearer private-value")
    monkeypatch.setattr(module, "MediaIoBaseDownload", Mock(return_value=download))
    with pytest.raises(ProviderError) as error:
        drive(tmp_path, Files()).get_bytes(metadata())
    assert "private-value" not in str(error.value)


@pytest.mark.parametrize("contents", ["private-value", json.dumps({"scopes": ["https://www.googleapis.com/auth/drive"]}),
                                     json.dumps({"scopes": SCOPES, "token_uri": "https://untrusted.example/token"})])
def test_invalid_token_never_starts_oauth_or_overwrites(tmp_path, contents):
    configured = config(tmp_path)
    configured.google_token_path.write_text(contents, encoding="utf-8")
    with pytest.raises(ConfigurationError) as error:
        DriveService(config=configured)._credentials()
    assert "private-value" not in str(error.value)
    assert configured.google_token_path.read_text(encoding="utf-8") == contents
    module.InstalledAppFlow.from_client_config.assert_not_called()


def test_refresh_failure_preserves_existing_token(tmp_path, monkeypatch):
    configured = config(tmp_path)
    original = json.dumps({"scopes": SCOPES})
    configured.google_token_path.write_text(original, encoding="utf-8")
    credentials = SimpleNamespace(expired=True, refresh_token="dummy", valid=False,
                                  refresh=Mock(side_effect=RuntimeError("private-value")))
    monkeypatch.setattr(module.Credentials, "from_authorized_user_info", Mock(return_value=credentials))
    with pytest.raises(ProviderError) as error:
        DriveService(config=configured)._credentials()
    assert "private-value" not in str(error.value)
    assert configured.google_token_path.read_text(encoding="utf-8") == original
    module.InstalledAppFlow.from_client_config.assert_not_called()
    assert credentials.refresh.call_args.args[0].timeout == configured.api_timeout_seconds


def test_valid_existing_token_is_not_rewritten(tmp_path, monkeypatch):
    configured = config(tmp_path)
    original = json.dumps({"scopes": SCOPES})
    configured.google_token_path.write_text(original, encoding="utf-8")
    credentials = SimpleNamespace(expired=False, valid=True)
    monkeypatch.setattr(module.Credentials, "from_authorized_user_info", Mock(return_value=credentials))
    assert DriveService(config=configured)._credentials() is credentials
    assert configured.google_token_path.read_text(encoding="utf-8") == original
    module.InstalledAppFlow.from_client_config.assert_not_called()


def test_desktop_oauth_is_readonly_loopback_and_has_bounded_http(tmp_path, monkeypatch):
    configured = config(tmp_path)
    configured.google_credentials_path.write_text(json.dumps({"installed": {
        "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token"
    }}), encoding="utf-8")
    credentials = SimpleNamespace(valid=True, to_json=lambda: json.dumps({"scopes": SCOPES}))
    http_request = Mock()
    flow = SimpleNamespace(oauth2session=SimpleNamespace(request=http_request))

    def authorize(**kwargs):
        flow.oauth2session.request("POST", "https://oauth2.googleapis.com/token", timeout=None)
        return credentials

    flow.run_local_server = Mock(side_effect=authorize)
    factory = Mock(return_value=flow)
    monkeypatch.setattr(module.InstalledAppFlow, "from_client_config", factory)
    assert DriveService(config=configured)._credentials() is credentials
    assert factory.call_args.args[1] == SCOPES
    assert flow.run_local_server.call_args.kwargs["host"] == "127.0.0.1"
    assert flow.run_local_server.call_args.kwargs["bind_addr"] == "127.0.0.1"
    assert flow.run_local_server.call_args.kwargs["timeout_seconds"] == configured.oauth_timeout_seconds
    assert http_request.call_args.kwargs["timeout"] == configured.api_timeout_seconds
    assert json.loads(configured.google_token_path.read_text(encoding="utf-8"))["scopes"] == SCOPES
    assert not list(tmp_path.glob(".oauth-*.tmp"))


def test_oauth_web_client_is_rejected(tmp_path):
    configured = config(tmp_path)
    configured.google_credentials_path.write_text(json.dumps({"web": {}}), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="Desktop"):
        DriveService(config=configured)._credentials()
    module.InstalledAppFlow.from_client_config.assert_not_called()


def test_atomic_token_failure_preserves_previous_and_cleans_temporary(tmp_path, monkeypatch):
    path = tmp_path / "token.json"
    path.write_text("previous", encoding="utf-8")
    monkeypatch.setattr(module.os, "replace", Mock(side_effect=OSError("private-value")))
    with pytest.raises(ConfigurationError) as error:
        module._save_token(path, SimpleNamespace(to_json=lambda: "new-value"))
    assert "private-value" not in str(error.value)
    assert path.read_text(encoding="utf-8") == "previous"
    assert not list(tmp_path.glob(".oauth-*.tmp"))


def test_client_http_uses_configured_timeout_and_retries(tmp_path, monkeypatch):
    service = DriveService(config=config(tmp_path))
    monkeypatch.setattr(service, "_credentials", Mock(return_value=object()))
    http = Mock(return_value=object())
    authorized = Mock(return_value=object())
    factory = Mock(return_value="fake-drive")
    monkeypatch.setattr(module.httplib2, "Http", http)
    monkeypatch.setattr(module, "AuthorizedHttp", authorized)
    monkeypatch.setattr(module, "build", factory)
    assert service._service() == "fake-drive"
    assert http.call_args.kwargs["timeout"] == 12
    assert factory.call_args.kwargs["num_retries"] == module.RETRIES
    assert authorized.call_args.kwargs["max_refresh_attempts"] == 1
    assert service._service() == "fake-drive"
    factory.assert_called_once()
