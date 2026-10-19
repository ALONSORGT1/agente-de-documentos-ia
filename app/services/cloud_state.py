"""Private durable journal with conditional writes and an expiring shared lease.

The small HTTP adapter follows Vercel's official Blob JS SDK protocol (API 13):
https://github.com/vercel/storage/tree/main/packages/blob/src
Python's Blob SDK currently lacks conditional PUT and uncached reads. Both are
essential here: a stale worker must never overwrite another worker's journal.
Only metadata and recovery state are stored; original documents stay in Drive.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import json
import re
import time
from uuid import uuid4

import httpx

from app.core.errors import BusyError, StateError

_current = ContextVar("document_cloud_state", default=None)
_MAX_BYTES = 8 * 1024 * 1024
_LEASE_SECONDS = 240  # Longer than the deployed function's 180 second hard limit.
_PATH = "document-agent/state.json"


def available(config):
    return bool(getattr(config, "is_cloud", False) and getattr(config, "blob_read_write_token", ""))


class CloudState:
    def __init__(self, config, http=None):
        token = config.blob_read_write_token
        match = re.fullmatch(r"vercel_blob_rw_([A-Za-z0-9]+)_[A-Za-z0-9]+", token)
        if not match:
            raise StateError("Falta una conexión válida al almacenamiento privado de la base documental.")
        self.http = http or httpx
        self.headers = {"Authorization": "Bearer " + token}
        self.url = f"https://{match[1].lower()}.private.blob.vercel-storage.com/{_PATH}"
        self.etag = None
        self.envelope = None
        self.owner = None

    def read(self):
        try:
            with self.http.stream("GET", self.url, params={"cache": "0"}, headers=self.headers,
                                  timeout=12, follow_redirects=False) as response:
                if response.status_code != 200:
                    raise StateError("No se pudo leer la base documental guardada. Revisa el almacenamiento privado.")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_BYTES:
                        raise ValueError()
                data = json.loads(body)
                etag = response.headers.get("etag")
                if (not etag or not isinstance(data, dict) or data.get("schema_version") != 1
                        or not isinstance(data.get("manifest"), dict)
                        or not isinstance(data.get("revision"), str)):
                    raise ValueError()
                lease = data.get("lease")
                if lease is not None and (not isinstance(lease, dict)
                        or not isinstance(lease.get("owner"), str)
                        or type(lease.get("expires")) not in {int, float}):
                    raise ValueError()
                self.etag, self.envelope = etag, data
                return deepcopy(data["manifest"])
        except StateError:
            raise
        except Exception:
            raise StateError("No se pudo leer de forma segura el estado guardado de la base documental.") from None

    def _write(self, data, *, create=False):
        data = deepcopy(data)
        data["revision"] = uuid4().hex  # Prevent identical-content ETags / ABA.
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
        if len(body) > _MAX_BYTES:
            raise StateError("La base documental supera el límite de almacenamiento configurado.")
        headers = {**self.headers, "x-api-version": "13", "x-vercel-blob-access": "private",
                   "x-content-type": "application/json", "x-add-random-suffix": "0",
                   "x-allow-overwrite": "0" if create else "1", "x-cache-control-max-age": "60"}
        if not create:
            if not self.etag:
                raise StateError("Falta la versión de la base documental; vuelve a cargar su estado.")
            headers["x-if-match"] = self.etag
        try:
            response = self.http.put("https://blob.vercel-storage.com/", params={"pathname": _PATH},
                                     headers=headers, content=body, timeout=12, follow_redirects=False)
            if response.status_code in {409, 412}:
                raise BusyError("La base documental cambió en otra operación. Actualiza el estado y vuelve a intentar.")
            if response.status_code != 200:
                raise StateError("No se confirmó el guardado de la base documental. Actualiza el estado antes de reintentar.")
            result = response.json()
            if not isinstance(result.get("etag"), str) or not result["etag"]:
                raise ValueError()
            self.etag, self.envelope = result["etag"], data
        except (StateError, BusyError):
            raise
        except Exception:
            # Never retry ambiguous writes or release a lease with a stale ETag.
            raise StateError("No se confirmó el guardado seguro de la base documental. Actualiza el estado antes de reintentar.") from None

    def initialize(self, manifest):
        self._write({"schema_version": 1, "manifest": manifest, "lease": None}, create=True)

    def acquire(self):
        self.read()
        lease = self.envelope.get("lease")
        if lease and lease["expires"] > time.time():
            raise BusyError("Hay una consulta o actualización en curso. Espera a que termine e inténtalo de nuevo.")
        owner = uuid4().hex
        data = deepcopy(self.envelope)
        data["lease"] = {"owner": owner, "expires": time.time() + _LEASE_SECONDS}
        self._write(data)
        self.owner = owner

    def save(self, manifest):
        lease = self.envelope.get("lease") if self.envelope else None
        if (not self.owner or not lease or lease["owner"] != self.owner
                or lease["expires"] <= time.time() + 20):
            raise StateError("La operación agotó su tiempo seguro. Actualiza el estado para continuar.")
        data = deepcopy(self.envelope)
        data["manifest"] = manifest
        self._write(data)

    def release(self):
        if self.owner:
            data = deepcopy(self.envelope)
            data["lease"] = None
            self._write(data)
            self.owner = None


def load(config):
    current = _current.get()
    return deepcopy(current.envelope["manifest"]) if current else CloudState(config).read()


def save(manifest):
    current = _current.get()
    if current is None:
        raise StateError("El guardado requiere una operación protegida de actualización.")
    current.save(manifest)


@contextmanager
def operation(config):
    if not available(config):
        yield
        return
    state = CloudState(config)
    state.acquire()
    context = _current.set(state)
    try:
        yield
    finally:
        _current.reset(context)
        try:
            state.release()
        except (StateError, BusyError):
            # A failed release expires safely. Never mask the original result.
            pass
