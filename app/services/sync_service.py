"""Journaled, incremental synchronization from a read-only Drive scope.

Only ``documents`` is searchable. Pending uploads and obsolete versions are
excluded even while provider deletion and indexing are eventually consistent.
"""
from hashlib import sha256
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from uuid import uuid4

from app.core.clients import get_openai_client
from app.core.config import settings
from app.core.errors import BusyError, DocumentError, ProviderError, StateError
from app.services.document_service import prepare_document
from app.services.drive_service import DriveService

MANIFEST = Path(__file__).resolve().parents[2] / "data" / "manifest.json"
_LOCK = threading.Lock()
SUPPORTED = {"application/pdf", "text/plain", "application/vnd.google-apps.document"}
SCHEMA_VERSION = 2
APP_OWNER = "agente-documental-tic"
_PENDING_STAGES = {"prepared", "uploading", "uploaded", "attaching", "indexing", "completed", "failed"}
_MAX_RECOVERY_PAGES = 100


class _ManifestWriteError(StateError):
    """Persistence failures must stop all subsequent provider side effects."""


def scope_fingerprint() -> str:
    scope = {"folder_id": settings.drive_folder_id,
             "include_subfolders": settings.drive_include_subfolders}
    return sha256(json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _identifier(value) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= 512 and not any(c.isspace() for c in value)


def _content_hash(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _metadata(item: dict) -> dict:
    drive_id = item["id"]
    return {
        "drive_id": drive_id, "name": item["name"], "modified_time": item.get("modifiedTime"),
        "mime_type": item["mimeType"], "drive_checksum": item.get("md5Checksum"),
        "drive_version": item.get("version"),
        "url": item.get("webViewLink") or (
            "https://docs.google.com/document/d/" + drive_id + "/edit"
            if item["mimeType"] == "application/vnd.google-apps.document"
            else "https://drive.google.com/file/d/" + drive_id + "/view"),
    }


def _safe_error(exc: Exception) -> str:
    # Provider messages may contain credentials, source text or full HTTP bodies.
    if isinstance(exc, (DocumentError, StateError, ProviderError)):
        return str(exc)
    return "No se pudo procesar el documento. Revisa conexión, permisos y estado del proveedor."


class SyncService:
    def __init__(self, client=None, drive=None):
        if getattr(settings, "is_cloud", False):
            # La nube consume una instantánea y nunca inicia OAuth ni proveedores de escritura.
            self.client, self.drive = client, drive
            return
        self.client = client or get_openai_client()
        self.drive = drive or DriveService()

    @staticmethod
    def _new_manifest() -> dict:
        return {"schema_version": SCHEMA_VERSION, "folder_id": settings.drive_folder_id,
                "scope_fingerprint": scope_fingerprint(), "vector_store_id": "",
                "store_creation_pending": False, "documents": {}, "pending": {},
                "cleanup": {}, "managed_file_ids": [], "update_intents": {}, "last_sync": None}

    @staticmethod
    def _validate_manifest(data: dict):
        problem = "El manifiesto es inválido; conserva una copia y realiza una reconciliación manual."
        if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != SCHEMA_VERSION:
            raise StateError("El manifiesto tiene un esquema antiguo o inválido; requiere reconciliación manual.")
        if (data.get("folder_id") != settings.drive_folder_id
                or data.get("scope_fingerprint") != scope_fingerprint()):
            raise StateError("La carpeta o el alcance cambió. Se requiere reconciliación manual del manifiesto.")
        store = data.get("vector_store_id")
        if not isinstance(store, str) or (store and not _identifier(store)):
            raise StateError(problem)
        if settings.vector_store_id and store and settings.vector_store_id != store:
            raise StateError("VECTOR_STORE_ID difiere del manifiesto; requiere reconciliación manual.")
        if not isinstance(data.get("store_creation_pending"), bool):
            raise StateError(problem)
        for key in ("documents", "pending", "cleanup"):
            if not isinstance(data.get(key), dict):
                raise StateError(problem)
        # Optional for earlier schema-2 manifests. A tombstone preserves the
        # operation kind when download/validation fails before an upload exists.
        intents = data.get("update_intents", {})
        if not isinstance(intents, dict) or any(
                not _identifier(drive_id) or value is not True or drive_id in data["documents"]
                for drive_id, value in intents.items()):
            raise StateError(problem)
        last_sync = data.get("last_sync")
        if last_sync is not None and (not isinstance(last_sync, dict)
                or not isinstance(last_sync.get("at"), str)
                or not isinstance(last_sync.get("stats"), dict)
                or not isinstance(last_sync.get("pending_count"), int)
                or last_sync["pending_count"] < 0):
            raise StateError(problem)
        managed = data.get("managed_file_ids")
        if not isinstance(managed, list) or any(not _identifier(i) for i in managed) or len(set(managed)) != len(managed):
            raise StateError(problem)
        owned, active = set(managed), set()

        def validate_document(drive_id, doc, indexed=True):
            if (not _identifier(drive_id) or not isinstance(doc, dict)
                    or doc.get("drive_id") != drive_id
                    or not isinstance(doc.get("name"), str) or not doc["name"]
                    or not isinstance(doc.get("mime_type"), str) or doc["mime_type"] not in SUPPORTED
                    or not isinstance(doc.get("url"), str)
                    or not (doc.get("modified_time") is None or isinstance(doc.get("modified_time"), str))
                    or not (doc.get("drive_checksum") is None or isinstance(doc.get("drive_checksum"), str))
                    or not (doc.get("drive_version") is None or isinstance(doc.get("drive_version"), str))):
                raise StateError(problem)
            if indexed and (not _identifier(doc.get("openai_file_id")) or doc["openai_file_id"] not in owned
                            or not _content_hash(doc.get("content_sha256"))):
                raise StateError(problem)

        for drive_id, doc in data["documents"].items():
            validate_document(drive_id, doc)
            if doc["openai_file_id"] in active:
                raise StateError(problem)
            active.add(doc["openai_file_id"])
        referenced = set(active)
        for drive_id, pending in data["pending"].items():
            if (drive_id in data["documents"] or not isinstance(pending, dict)
                    or not isinstance(pending.get("stage"), str) or pending["stage"] not in _PENDING_STAGES
                    or not _identifier(pending.get("operation_id"))
                    or not isinstance(pending.get("upload_filename"), str)
                    or not pending["upload_filename"].startswith("tic_" + pending["operation_id"] + "_")
                    or any(c in pending["upload_filename"] for c in ("/", "\\"))
                    or not isinstance(pending.get("cancelled"), bool)
                    or not isinstance(pending.get("kind"), str) or pending["kind"] not in {"added", "updated"}):
                raise StateError(problem)
            validate_document(drive_id, pending.get("source"), indexed=False)
            if not _content_hash(pending.get("content_sha256")):
                raise StateError(problem)
            file_id = pending.get("openai_file_id")
            if not isinstance(file_id, str) or (file_id and file_id not in owned):
                raise StateError(problem)
            if file_id and file_id in referenced:
                raise StateError(problem)
            if file_id:
                referenced.add(file_id)
            if pending["stage"] not in {"prepared", "uploading"} and not file_id:
                raise StateError(problem)
            if pending["stage"] in {"prepared", "uploading"} and file_id:
                raise StateError(problem)
        for file_id, record in data["cleanup"].items():
            if file_id not in owned or file_id in referenced or not isinstance(record, dict) or not isinstance(record.get("detached"), bool):
                raise StateError(problem)
        if (owned or data["documents"] or data["pending"] or data["cleanup"] or intents) and not store:
            raise StateError(problem)

    @staticmethod
    def load_manifest() -> dict:
        if getattr(settings, "is_cloud", False):
            snapshot = settings.document_manifest_json
            if not snapshot.strip():
                raise StateError("Falta DOCUMENT_MANIFEST_JSON. Sincroniza localmente y configura la instantánea en Vercel.")
            if len(snapshot.encode("utf-8")) > 8 * 1024 * 1024:
                raise StateError("La instantánea de documentos excede el tamaño permitido.")
            try:
                data = json.loads(snapshot)
            except (ValueError, RecursionError):
                raise StateError("DOCUMENT_MANIFEST_JSON no contiene una instantánea JSON válida.") from None
            SyncService._validate_manifest(data)
            return data
        if not MANIFEST.exists():
            return SyncService._new_manifest()
        try:
            if MANIFEST.stat().st_size > 8 * 1024 * 1024:
                raise StateError("El manifiesto excede el tamaño permitido; requiere revisión manual.")
            data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise StateError("No se pudo leer el manifiesto; conserva el archivo y realiza una reconciliación manual.") from exc
        SyncService._validate_manifest(data)
        return data

    @staticmethod
    def save_manifest(data: dict):
        if getattr(settings, "is_cloud", False):
            raise StateError("La nube no guarda cambios del manifiesto. Sincroniza localmente y renueva DOCUMENT_MANIFEST_JSON en Vercel.")
        try:
            SyncService._validate_manifest(data)
        except StateError as exc:
            raise _ManifestWriteError(str(exc)) from exc
        path = None
        try:
            MANIFEST.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=MANIFEST.parent,
                                             prefix=".manifest-", suffix=".tmp", delete=False) as fh:
                path = Path(fh.name)
                json.dump(data, fh, ensure_ascii=False, indent=2, allow_nan=False)
                fh.flush()
                os.fsync(fh.fileno())
            # OneDrive/antivirus can briefly open the destination without delete
            # sharing on Windows. Keep the old manifest intact while retrying.
            for attempt in range(6):
                try:
                    os.replace(path, MANIFEST)
                    break
                except PermissionError:
                    if attempt == 5:
                        raise
                    time.sleep(0.05 * (2 ** attempt))
        except (OSError, ValueError) as exc:
            raise _ManifestWriteError("No se pudo guardar el estado de sincronización de forma segura.") from exc
        finally:
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    def get_store(self, manifest):
        self._validate_manifest(manifest)
        vector_id = manifest["vector_store_id"] or settings.vector_store_id
        if vector_id:
            try:
                remote = self.client.vector_stores.retrieve(vector_store_id=vector_id)
            except Exception as exc:
                raise ProviderError("No se pudo verificar el Vector Store configurado. No se reemplazó.") from exc
            metadata = getattr(remote, "metadata", None) or {}
            if metadata.get("app_owner") != APP_OWNER or metadata.get("app_scope") != scope_fingerprint():
                raise StateError("El Vector Store no pertenece al alcance configurado; requiere reconciliación manual.")
            if manifest["vector_store_id"] != vector_id:
                manifest["vector_store_id"] = vector_id
                self.save_manifest(manifest)
            return vector_id
        if manifest["store_creation_pending"]:
            raise StateError("Una creación previa de Vector Store quedó sin confirmar; requiere reconciliación manual.")
        manifest["store_creation_pending"] = True
        self.save_manifest(manifest)
        try:
            created = self.client.with_options(max_retries=0).vector_stores.create(name="Agente de documentos IA - Documentos", metadata={
                "app_owner": APP_OWNER, "app_scope": scope_fingerprint()})
        except Exception as exc:
            raise ProviderError("No se pudo confirmar la creación del Vector Store; revisa su estado antes de reintentar.") from exc
        manifest["vector_store_id"] = created.id
        manifest["store_creation_pending"] = False
        self.save_manifest(manifest)
        return created.id

    @staticmethod
    def _queue_cleanup(manifest, file_id):
        if file_id:
            if file_id not in manifest["managed_file_ids"]:
                raise StateError("Se rechazó eliminar un archivo sin registro de propiedad de la aplicación.")
            manifest["cleanup"].setdefault(file_id, {"detached": False})

    def _recover_upload(self, manifest, pending):
        """Reconcile a lost upload response by its precommitted unique filename."""
        matches = []
        try:
            page = self.client.files.list(purpose="assistants", limit=100)
            for page_number in range(_MAX_RECOVERY_PAGES):
                matches.extend(f.id for f in page.data if f.filename == pending["upload_filename"])
                if not page.has_next_page():
                    break
                if page_number == _MAX_RECOVERY_PAGES - 1:
                    raise ProviderError("La reconciliación excede el límite de páginas; requiere revisión manual.")
                page = page.get_next_page()
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("No se pudo reconciliar la carga pendiente; no se creó otra copia.") from exc
        if len(matches) != 1:
            raise StateError("Carga pendiente sin confirmación única; requiere reconciliación manual para evitar duplicados.")
        pending["openai_file_id"] = matches[0]
        if matches[0] not in manifest["managed_file_ids"]:
            manifest["managed_file_ids"].append(matches[0])
        pending["stage"] = "uploaded"
        self.save_manifest(manifest)

    @staticmethod
    def _not_found(exc):
        return getattr(exc, "status_code", None) == 404

    def _index_pending(self, manifest, pending, payload=None):
        store = manifest["vector_store_id"]
        if pending["stage"] == "uploading":
            self._recover_upload(manifest, pending)
        if pending["stage"] == "prepared":
            if payload is None:
                raise StateError("La carga pendiente necesita volver a preparar el documento.")
            pending["stage"] = "uploading"
            self.save_manifest(manifest)
            # Do not let the SDK retry an ambiguous non-idempotent upload.
            uploader = self.client.with_options(max_retries=0)
            uploaded = uploader.files.create(file=(pending["upload_filename"], payload.content), purpose="assistants")
            pending["openai_file_id"] = uploaded.id
            manifest["managed_file_ids"].append(uploaded.id)
            pending["stage"] = "uploaded"
            self.save_manifest(manifest)
        file_id = pending.get("openai_file_id")
        if pending["cancelled"]:
            return
        if pending["stage"] == "failed":
            try:
                self.client.vector_stores.files.delete(vector_store_id=store, file_id=file_id)
            except Exception as exc:
                if not self._not_found(exc):
                    raise
            pending["stage"] = "uploaded"
            self.save_manifest(manifest)
        if pending["stage"] == "uploaded":
            pending["stage"] = "attaching"
            self.save_manifest(manifest)
            try:
                self.client.vector_stores.files.create(vector_store_id=store, file_id=file_id,
                    attributes={"app_file_id": file_id, "drive_id": pending["source"]["drive_id"]})
            except Exception as exc:
                raise ProviderError("No se confirmó la asociación al índice; la carga quedó pendiente.") from exc
            pending["stage"] = "indexing"
            self.save_manifest(manifest)
        deadline = time.monotonic() + settings.index_poll_timeout_seconds
        while pending["stage"] in {"attaching", "indexing"}:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError("La indexación sigue en proceso; vuelve a sincronizar para confirmar su finalización.")
            poll_client = self.client.with_options(timeout=min(settings.api_timeout_seconds, remaining), max_retries=0)
            try:
                remote = poll_client.vector_stores.files.retrieve(vector_store_id=store, file_id=file_id)
            except Exception as exc:
                if self._not_found(exc) and pending["stage"] == "attaching":
                    # A Vector Store attachment is unique by the uploaded file ID.
                    self.client.vector_stores.files.create(vector_store_id=store, file_id=file_id,
                        attributes={"app_file_id": file_id, "drive_id": pending["source"]["drive_id"]})
                    pending["stage"] = "indexing"
                    self.save_manifest(manifest)
                    # Match the normal attach path: polling starts after the
                    # attachment and its durable journal commit. Slow local
                    # fsync/Windows sharing retries must not exhaust the index
                    # budget before the first actual status retrieval.
                    deadline = time.monotonic() + settings.index_poll_timeout_seconds
                    continue
                raise ProviderError("No se pudo confirmar la indexación; el documento sigue pendiente.") from exc
            status = getattr(remote, "status", None)
            if status == "completed":
                pending["stage"] = "completed"
                self.save_manifest(manifest)
                break
            if status in {"failed", "cancelled"}:
                pending["stage"] = "failed"
                self.save_manifest(manifest)
                raise ProviderError("La indexación falló. El documento sigue pendiente y no está disponible para consultas.")
            if status not in {"in_progress", "queued"}:
                raise ProviderError("El proveedor devolvió un estado de indexación desconocido.")
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))

    def _cleanup(self, manifest, stats):
        store = manifest["vector_store_id"]
        for file_id, record in list(manifest["cleanup"].items()):
            try:
                if not record["detached"]:
                    try:
                        self.client.vector_stores.files.delete(vector_store_id=store, file_id=file_id)
                    except Exception as exc:
                        if not self._not_found(exc):
                            raise
                    record["detached"] = True
                    self.save_manifest(manifest)
                try:
                    self.client.files.delete(file_id)
                except Exception as exc:
                    if not self._not_found(exc):
                        raise
                del manifest["cleanup"][file_id]
                manifest["managed_file_ids"].remove(file_id)
                self.save_manifest(manifest)
            except StateError:
                raise
            except Exception:
                stats["errors"].append({"file": file_id, "error": "La limpieza remota sigue pendiente; el archivo ya está excluido de las consultas."})

    def _finish(self, manifest, stats):
        pending_count = len(manifest["pending"])
        cleanup_count = len(manifest["cleanup"])
        manifest["last_sync"] = {"at": datetime.now(timezone.utc).isoformat(), "stats": stats,
                                 "pending_count": pending_count, "cleanup_count": cleanup_count}
        self.save_manifest(manifest)
        return {"vector_store_id": manifest["vector_store_id"], "stats": stats,
                "documents": list(manifest["documents"].values()), "pending_count": pending_count,
                "cleanup_count": cleanup_count}

    def sync(self):
        if getattr(settings, "is_cloud", False):
            raise StateError("La sincronización se realiza localmente. Renueva DOCUMENT_MANIFEST_JSON en Vercel al terminar.")
        if not _LOCK.acquire(blocking=False):
            raise BusyError("Hay una consulta o sincronización en curso. Intenta de nuevo al finalizar.")
        try:
            manifest = self.load_manifest()
            source = self.drive.list_files(settings.drive_folder_id)
            eligible = {}
            for item in source:
                if not isinstance(item, dict) or not _identifier(item.get("id")) or not isinstance(item.get("name"), str) or not item["name"]:
                    raise ProviderError("Drive devolvió metadatos inválidos; se canceló la sincronización.")
                if item.get("mimeType") in SUPPORTED:
                    if item["id"] in eligible:
                        raise ProviderError("Drive devolvió identificadores duplicados; se canceló la sincronización.")
                    eligible[item["id"]] = item
            stats = {"added": 0, "updated": 0, "renamed": 0, "removed": 0, "unchanged": 0,
                     "skipped": len(source) - len(eligible), "errors": []}
            if not eligible and not manifest["vector_store_id"] and not settings.vector_store_id:
                if manifest["store_creation_pending"]:
                    raise StateError("Una creación previa de Vector Store necesita reconciliación manual.")
                return self._finish(manifest, stats)
            self.get_store(manifest)
            docs, pending_map = manifest["documents"], manifest["pending"]
            update_intents = manifest.setdefault("update_intents", {})
            for drive_id, pending in pending_map.items():
                if pending["kind"] == "updated":
                    update_intents[drive_id] = True
            for drive_id in set(update_intents) - set(eligible):
                del update_intents[drive_id]
                if drive_id not in docs and drive_id not in pending_map:
                    stats["removed"] += 1
                self.save_manifest(manifest)
            # Persist access revocation before any eventually consistent deletion.
            for drive_id in set(docs) - set(eligible):
                old = docs.pop(drive_id)
                self._queue_cleanup(manifest, old["openai_file_id"])
                stats["removed"] += 1
                self.save_manifest(manifest)
            for drive_id, pending in list(pending_map.items()):
                item = eligible.get(drive_id)
                if item is None or any(pending["source"].get(k) != _metadata(item).get(k)
                                       for k in ("modified_time", "mime_type", "drive_checksum", "drive_version")):
                    pending["cancelled"] = True
                    self.save_manifest(manifest)
                if pending["cancelled"]:
                    try:
                        if pending["stage"] == "uploading":
                            self._recover_upload(manifest, pending)
                        self._queue_cleanup(manifest, pending.get("openai_file_id"))
                        del pending_map[drive_id]
                        self.save_manifest(manifest)
                        if item is None:
                            stats["removed"] += 1
                    except _ManifestWriteError:
                        raise
                    except Exception as exc:
                        stats["errors"].append({"file": pending["source"]["name"], "error": _safe_error(exc)})
            for drive_id, item in eligible.items():
                previous = docs.get(drive_id)
                current_metadata = _metadata(item)
                pending = pending_map.get(drive_id)
                if pending and pending["cancelled"]:
                    continue
                try:
                    same_revision = previous and previous["modified_time"] and all(
                        previous.get(k) == current_metadata.get(k)
                        for k in ("modified_time", "mime_type", "drive_checksum", "drive_version"))
                    same_checksum = (previous and previous.get("drive_checksum")
                                     and previous["drive_checksum"] == current_metadata["drive_checksum"]
                                     and previous["mime_type"] == current_metadata["mime_type"])
                    if same_revision or same_checksum:
                        renamed = previous["name"] != current_metadata["name"]
                        if any(previous.get(k) != v for k, v in current_metadata.items()):
                            previous.update(current_metadata)
                            self.save_manifest(manifest)
                        stats["renamed" if renamed else "unchanged"] += 1
                        continue
                    payload = None
                    if not pending or pending["stage"] == "prepared":
                        # Failed downloads must not leave a stale version active.
                        if previous:
                            del docs[drive_id]
                            update_intents[drive_id] = True
                            self._queue_cleanup(manifest, previous["openai_file_id"])
                            self.save_manifest(manifest)
                        raw, filename, mime_type = self.drive.get_bytes(item)
                        payload = prepare_document(raw, filename, mime_type, settings.max_file_bytes)
                        if previous and previous["content_sha256"] == payload.content_sha256:
                            renamed = previous["name"] != current_metadata["name"]
                            manifest["cleanup"].pop(previous["openai_file_id"], None)
                            previous.update(current_metadata)
                            update_intents.pop(drive_id, None)
                            docs[drive_id] = previous
                            self.save_manifest(manifest)
                            stats["renamed" if renamed else "unchanged"] += 1
                            continue
                        if pending and pending["content_sha256"] != payload.content_sha256:
                            del pending_map[drive_id]
                            pending = None
                        if not pending:
                            operation = uuid4().hex
                            pending = {"operation_id": operation, "stage": "prepared", "cancelled": False,
                                       "upload_filename": "tic_" + operation + "_" + payload.filename,
                                       "content_sha256": payload.content_sha256, "source": current_metadata,
                                       "openai_file_id": "", "kind": "updated" if drive_id in update_intents else "added"}
                            pending_map[drive_id] = pending
                            self.save_manifest(manifest)
                    self._index_pending(manifest, pending, payload)
                    if pending["stage"] == "completed":
                        update_intents.pop(drive_id, None)
                        docs[drive_id] = {**current_metadata, "content_sha256": pending["content_sha256"],
                                          "openai_file_id": pending["openai_file_id"]}
                        del pending_map[drive_id]
                        self.save_manifest(manifest)
                        stats[pending["kind"]] += 1
                except _ManifestWriteError:
                    raise
                except Exception as exc:
                    stats["errors"].append({"file": item["name"], "error": _safe_error(exc)})
            self._cleanup(manifest, stats)
            return self._finish(manifest, stats)
        finally:
            _LOCK.release()
