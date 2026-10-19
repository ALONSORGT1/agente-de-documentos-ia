from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.errors import BusyError, StateError, ConfigurationError
from app.services import cloud_state, sync_service as sync, drive_service
import app.main as main

TOKEN = 'vercel_blob_rw_teststore_testcredential'


class Store:
    def __init__(self):
        self.body = None
        self.etag = None
        self.requests = []
        self.fail_write = False
        self.weak = False
        self.http = httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request):
        self.requests.append(request)
        if request.method == 'GET':
            assert request.url.params['cache'] == '0'
            assert request.headers['accept-encoding'] == 'identity'
            return httpx.Response(404 if self.body is None else 200, content=self.body or b'', headers={'etag': ('W/' if self.weak else '') + (self.etag or '')})
        if self.fail_write:
            return httpx.Response(503)
        if self.body is not None and request.headers.get('x-if-match') != self.etag:
            return httpx.Response(412)
        self.body = request.content
        self.etag = '"' + sha256(self.body).hexdigest() + '"'
        return httpx.Response(200, json={'etag': self.etag})


@pytest.fixture
def remote(monkeypatch):
    config = Settings(deployment_mode='cloud', blob_read_write_token=TOKEN, google_token_json='private-google-token',
                      app_access_key='private-access', drive_folder_id='authorized-folder-123', vector_store_id='')
    monkeypatch.setattr(sync, 'settings', config)
    monkeypatch.setattr(main, 'settings', config)
    store = Store()
    original = cloud_state.CloudState
    monkeypatch.setattr(cloud_state, 'CloudState', lambda value: original(value, http=store.http))
    state = cloud_state.CloudState(config)
    state.initialize(sync.SyncService._new_manifest())
    yield config, store, state
    store.http.close()


def test_private_uncached_reads_and_conditional_writes_reject_stale_worker(remote):
    config, store, state = remote
    stale = cloud_state.CloudState(config)
    stale.read()
    state.acquire()
    with pytest.raises(BusyError):
        stale._write(stale.envelope)
    with pytest.raises(BusyError):
        cloud_state.CloudState(config).acquire()
    state.save({'saved': True})
    state.release()
    assert stale.read() == {'saved': True}
    assert all(request.headers['Authorization'] == 'Bearer ' + TOKEN for request in store.requests)
    assert all(request.headers.get('x-vercel-blob-access') == 'private' for request in store.requests if request.method == 'PUT')


def test_expired_lease_can_be_recovered_but_old_writer_cannot_save(remote):
    config, _, state = remote
    state.acquire()
    state.envelope['lease']['expires'] = 1
    state._write(state.envelope)
    other = cloud_state.CloudState(config)
    other.acquire()
    with pytest.raises(StateError):
        state.save({'old': True})
    other.release()


def test_read_failure_does_not_fall_back_to_old_environment_snapshot(remote):
    config, store, _ = remote
    store.body = b'private-invalid-json'
    with pytest.raises(StateError) as error:
        sync.SyncService.load_manifest()
    assert 'private-invalid-json' not in str(error.value)


def test_compressed_weak_etag_is_never_used_for_a_conditional_write(remote):
    config, store, _ = remote
    store.weak = True
    with pytest.raises(StateError):
        cloud_state.CloudState(config).acquire()


def test_cloud_batches_keep_old_files_and_download_only_new_files(remote):
    from test_sync_service import FakeClient, FakeDrive, item
    config, _, _ = remote
    client = FakeClient()
    drive = FakeDrive([item('drive-file-001'), item('drive-file-002'), item('drive-file-003')])
    service = sync.SyncService(client=client, drive=drive)
    first = service.sync()
    assert first['has_more'] and first['stats']['added'] == 2
    second = sync.SyncService(client=client, drive=drive).sync()
    assert not second['has_more'] and second['stats']['added'] == 1
    assert second['stats']['unchanged'] == 2
    assert len(drive.downloads) == client.files.creates == 3
    assert sync.SyncService(client=client, drive=drive).sync()['stats']['unchanged'] == 3
    assert len(drive.downloads) == client.files.creates == 3


def test_failed_cloud_persistence_stops_before_provider_mutations(remote):
    from test_sync_service import FakeClient, FakeDrive, item
    _, store, _ = remote
    client = FakeClient()
    drive = FakeDrive([item()])
    store.fail_write = True
    with pytest.raises(StateError):
        sync.SyncService(client=client, drive=drive).sync()
    assert client.files.creates == client.vector_stores.creates == 0
    assert drive.list_calls == 0


def test_cloud_refreshes_read_only_google_token_without_oauth_or_disk(remote, monkeypatch):
    config, _, _ = remote
    token = json.dumps({'scopes': drive_service.SCOPES, 'token_uri': 'https://oauth2.googleapis.com/token'})
    credentials = Mock(valid=False, refresh_token='private-refresh')
    credentials.refresh.side_effect = lambda request: setattr(credentials, 'valid', True)
    credentials_factory = Mock(return_value=credentials)
    monkeypatch.setattr(drive_service.Credentials, 'from_authorized_user_info', credentials_factory)
    monkeypatch.setattr(drive_service, '_save_token', Mock(side_effect=AssertionError('disk')))
    monkeypatch.setattr(drive_service.InstalledAppFlow, 'from_client_config', Mock(side_effect=AssertionError('OAuth')))
    assert drive_service.DriveService(config=replace(config, google_token_json=token))._credentials() is credentials
    credentials.refresh.assert_called_once()
    with pytest.raises(ConfigurationError):
        drive_service.DriveService(config=replace(config, google_token_json=token.replace('drive.readonly', 'drive')))._credentials()


def test_cookie_session_survives_reload_expires_and_cannot_be_forged(remote):
    config, _, _ = remote
    main._requests.clear()
    headers = {'X-Requested-With': 'AgenteDocumentalTIC', 'X-App-Key': config.app_access_key}
    with TestClient(main.app, base_url='https://agent.example', client=('203.0.113.8', 4333)) as client:
        assert client.get('/api/documents').status_code == 401
        result = client.post('/api/session', json={}, headers=headers)
        assert result.status_code == 200
        cookie = result.headers['set-cookie']
        assert 'HttpOnly' in cookie and 'Secure' in cookie and 'SameSite=strict' in cookie
        assert config.app_access_key not in cookie
        assert client.get('/api/health').json()['authenticated'] is True
        assert client.get('/api/documents').status_code == 200
        assert client.get('/api/documents', headers={'Origin': 'https://evil.example'}).status_code == 403
        assert client.post('/api/logout', json={}, headers=headers).status_code == 200
        assert client.get('/api/documents').status_code == 401
        client.cookies.set('document_agent_session', '1.nonce.forged')
        assert client.get('/api/documents').status_code == 401
