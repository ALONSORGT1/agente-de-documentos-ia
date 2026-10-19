"""Drive de solo lectura, limitado a una carpeta y con OAuth diferido.

Las pruebas pueden inyectar ``service`` o ``client`` sin credenciales reales.
"""
from collections import deque
from io import BytesIO
import json
import os
from pathlib import Path
import re
import tempfile

import httplib2
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from app.core.config import settings
from app.core.errors import ConfigurationError, DocumentError, ProviderError

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
FOLDER_MIME = "application/vnd.google-apps.folder"
DOC_MIME = "application/vnd.google-apps.document"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
SUPPORTED = {"application/pdf", "text/plain", DOC_MIME,
             "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
FILE_FIELDS = "id,name,mimeType,modifiedTime,webViewLink,size,md5Checksum,version,parents,trashed"
ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{10,200}\Z")
MAX_DEPTH = 20
RETRIES = 2


def _valid_id(value: str, label: str = "carpeta") -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ConfigurationError(f"El identificador de {label} de Google Drive no es válido.")
    return value


class _LimitedBytesIO(BytesIO):
    def __init__(self, limit: int):
        super().__init__()
        self.limit = limit

    def write(self, data):
        if self.tell() + len(data) > self.limit:
            raise DocumentError("El documento supera el tamaño máximo permitido.")
        return super().write(data)


class _BoundedRequest(Request):
    def __init__(self, timeout: float):
        super().__init__()
        self.timeout = timeout

    def __call__(self, *args, **kwargs):
        kwargs["timeout"] = self.timeout
        return super().__call__(*args, **kwargs)


def _save_token(path: Path, credentials) -> None:
    """Reemplazo atómico; un fallo conserva el token anterior completo."""
    temporary = None
    try:
        if path.is_symlink():
            raise ConfigurationError("La ruta del token OAuth no puede ser un enlace simbólico.")
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".oauth-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            if os.name == "posix":
                try:
                    os.chmod(temporary, 0o600)
                except OSError:
                    pass
            handle.write(credentials.to_json())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        if os.name == "posix":
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
    except ConfigurationError:
        raise
    except Exception:
        raise ConfigurationError("No se pudo guardar de forma segura el token OAuth.") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class DriveService:
    def __init__(self, service=None, *, client=None, config=None):
        if service is not None and client is not None:
            raise ValueError("Inyecta service o client, no ambos.")
        self.service = service if service is not None else client
        self.config = config if config is not None else settings
        self._folder_id = None

    def _root(self, folder_id: str | None = None) -> str:
        configured = self.config.drive_folder_id
        if configured:
            configured = _valid_id(configured)
        value = _valid_id(folder_id if folder_id is not None else (self._folder_id or configured))
        if configured and configured != value:
            raise ConfigurationError("La carpeta solicitada no coincide con la carpeta autorizada.")
        return value

    def _credentials(self):
        if getattr(self.config, "is_cloud", False):
            # Authorized read-only credentials stay on the server. Cloud never
            # opens a localhost OAuth flow or writes a token into its filesystem.
            try:
                data = json.loads(self.config.google_token_json)
                scopes = data.get("scopes", [])
                if isinstance(scopes, str):
                    scopes = scopes.split()
                if (set(scopes) != set(SCOPES)
                        or data.get("token_uri") != "https://oauth2.googleapis.com/token"):
                    raise ValueError()
                credentials = Credentials.from_authorized_user_info(data, SCOPES)
            except Exception:
                raise ConfigurationError("Falta una autorización de Google Drive de solo lectura válida en el servidor.") from None
            if not credentials.valid and credentials.refresh_token:
                try:
                    credentials.refresh(_BoundedRequest(min(self.config.api_timeout_seconds, 15)))
                except Exception:
                    raise ProviderError("La autorización de Drive necesita renovarse. Revisa la conexión de Google del servidor.") from None
            if not credentials.valid:
                raise ConfigurationError("La autorización de Drive venció. Renueva la conexión de Google del servidor.")
            return credentials
        token = Path(self.config.google_token_path)
        credentials_file = Path(self.config.google_credentials_path)
        if token.exists() or token.is_symlink():
            try:
                if token.is_symlink():
                    raise ValueError()
                data = json.loads(token.read_text(encoding="utf-8"))
                scopes = data.get("scopes", [])
                if isinstance(scopes, str):
                    scopes = scopes.split()
                if set(scopes) != set(SCOPES):
                    raise ValueError()
                if data.get("token_uri", "https://oauth2.googleapis.com/token") != "https://oauth2.googleapis.com/token":
                    raise ValueError()
                credentials = Credentials.from_authorized_user_info(data, SCOPES)
            except Exception:
                raise ConfigurationError(
                    "El token OAuth existente es inválido o no es de solo lectura. "
                    "Revísalo y autoriza de nuevo explícitamente; se conserva sin cambios."
                ) from None
            if credentials.expired and credentials.refresh_token:
                try:
                    credentials.refresh(_BoundedRequest(self.config.api_timeout_seconds))
                except Exception:
                    raise ProviderError(
                        "No se pudo renovar OAuth de Google. Revisa la autorización y la conexión; "
                        "el token existente se conserva."
                    ) from None
                if not credentials.valid:
                    raise ProviderError("Google no devolvió una autorización válida; se conserva el token anterior.")
                _save_token(token, credentials)
            if not credentials.valid:
                raise ConfigurationError(
                    "La autorización de Google ya no es válida. Reautoriza explícitamente; "
                    "el token existente se conserva."
                )
            return credentials
        try:
            data = json.loads(credentials_file.read_text(encoding="utf-8"))
            desktop = data["installed"]
            if "web" in data or desktop["token_uri"] != "https://oauth2.googleapis.com/token":
                raise ValueError()
            if desktop["auth_uri"] not in {
                "https://accounts.google.com/o/oauth2/auth", "https://accounts.google.com/o/oauth2/v2/auth"
            }:
                raise ValueError()
            flow = InstalledAppFlow.from_client_config(data, SCOPES)
        except Exception:
            raise ConfigurationError(
                "Falta una configuración OAuth Desktop válida de Google. Configura GOOGLE_CREDENTIALS_PATH."
            ) from None
        # El callback local y el intercambio HTTP del token necesitan límites
        # independientes: run_local_server no impone timeout al segundo.
        original_request = flow.oauth2session.request

        def bounded_request(*args, **kwargs):
            kwargs["timeout"] = self.config.api_timeout_seconds
            return original_request(*args, **kwargs)

        flow.oauth2session.request = bounded_request
        try:
            credentials = flow.run_local_server(
                host="127.0.0.1", bind_addr="127.0.0.1", port=0, open_browser=True,
                timeout_seconds=self.config.oauth_timeout_seconds,
                authorization_prompt_message="", success_message="Autorización completada. Puedes cerrar esta ventana.",
            )
        except Exception:
            raise ProviderError("No se completó OAuth de Google dentro del plazo. Reintenta la autorización local.") from None
        if not credentials.valid:
            raise ProviderError("Google no devolvió una autorización válida.")
        _save_token(token, credentials)
        return credentials

    def _service(self):
        if self.service is None:
            credentials = self._credentials()
            try:
                timeout = min(self.config.api_timeout_seconds, 15) if getattr(self.config, "is_cloud", False) else self.config.api_timeout_seconds
                http = AuthorizedHttp(credentials, http=httplib2.Http(timeout=timeout),
                                      max_refresh_attempts=1)
                self.service = build("drive", "v3", http=http, cache_discovery=False, num_retries=RETRIES)
            except Exception:
                raise ProviderError("No se pudo iniciar el cliente de Google Drive.") from None
        return self.service

    @staticmethod
    def _execute(request):
        try:
            result = request.execute(num_retries=RETRIES)
        except Exception:
            raise ProviderError("Google Drive no respondió. Revisa conexión, permisos y autorización.") from None
        if not isinstance(result, dict):
            raise ProviderError("Google Drive devolvió una respuesta no válida.")
        return result

    def _metadata(self, file_id: str) -> dict:
        item = self._execute(self._service().files().get(
            fileId=_valid_id(file_id, "archivo"), fields=FILE_FIELDS, supportsAllDrives=True,
        ))
        self._validate_metadata(item)
        if item["id"] != file_id:
            raise ProviderError("Los metadatos recibidos de Drive no corresponden al archivo solicitado.")
        return item

    @staticmethod
    def _validate_metadata(item):
        if not isinstance(item, dict):
            raise ProviderError("Drive devolvió metadatos incompletos.")
        try:
            _valid_id(item["id"], "archivo")
            if (not isinstance(item["name"], str) or not isinstance(item["mimeType"], str)
                    or not isinstance(item["modifiedTime"], str) or not item["modifiedTime"]):
                raise ValueError()
            if not isinstance(item.get("parents", []), list):
                raise ValueError()
            if any(not isinstance(parent, str) or not ID_PATTERN.fullmatch(parent) for parent in item.get("parents", [])):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise ProviderError("Drive devolvió metadatos incompletos.") from None

    def _folder(self, folder_id: str) -> dict:
        item = self._metadata(folder_id)
        if item["mimeType"] != FOLDER_MIME or item.get("trashed", False):
            raise ConfigurationError("La carpeta autorizada no es una carpeta válida y disponible de Drive.")
        return item

    def _in_scope(self, item: dict, root: str):
        if item.get("trashed", False):
            raise DocumentError("El archivo está en la papelera de Drive.")
        parents = item.get("parents", [])
        visited = {item["id"]}
        for _ in range(MAX_DEPTH + 1):
            if len(parents) != 1:
                break
            parent = parents[0]
            if parent == root:
                self._folder(root)
                return
            if not self.config.drive_include_subfolders or parent in visited:
                break
            visited.add(parent)
            folder = self._folder(parent)
            parents = folder.get("parents", [])
        raise DocumentError("El documento ya no pertenece a la carpeta autorizada o excede su alcance.")

    def list_files(self, folder_id: str) -> list[dict]:
        root = self._root(folder_id)
        self._folder(root)
        queue = deque([(root, 0)])
        folders = {root}
        seen = set()
        files = []
        page_count = 0
        while queue:
            current, depth = queue.popleft()
            if current != root:
                self._in_scope(self._folder(current), root)
            page = None
            pages = set()
            while True:
                page_count += 1
                if page_count > self.config.drive_max_files + MAX_DEPTH + 1:
                    raise ProviderError("La paginación de Drive excede el límite seguro; el listado está incompleto.")
                result = self._execute(self._service().files().list(
                    q=f"'{current}' in parents and trashed = false", pageSize=100, pageToken=page,
                    fields=f"nextPageToken,incompleteSearch,files({FILE_FIELDS})", orderBy="name",
                    supportsAllDrives=True, includeItemsFromAllDrives=True,
                ))
                if result.get("incompleteSearch", False) or not isinstance(result.get("files", []), list):
                    raise ProviderError("El listado de Drive está incompleto; no se aplicaron bajas documentales.")
                for item in result.get("files", []):
                    self._validate_metadata(item)
                    if item["id"] in seen:
                        raise ProviderError("Drive devolvió archivos repetidos; reintenta el listado completo.")
                    seen.add(item["id"])
                    if len(seen) > self.config.drive_max_files:
                        raise ProviderError("La carpeta supera DRIVE_MAX_FILES; el listado no se considera completo.")
                    if item.get("trashed", False) or item.get("parents") != [current]:
                        raise ProviderError("La carpeta cambió durante el listado. Reintenta la sincronización.")
                    mime = item["mimeType"]
                    if mime == FOLDER_MIME and self.config.drive_include_subfolders:
                        if depth >= MAX_DEPTH or item["id"] in folders:
                            raise ProviderError("La estructura de carpetas excede el límite seguro de recorrido.")
                        folders.add(item["id"])
                        queue.append((item["id"], depth + 1))
                    elif mime not in {FOLDER_MIME, SHORTCUT_MIME}:
                        files.append(item)
                next_page = result.get("nextPageToken")
                if next_page is None or next_page == "":
                    break
                if not isinstance(next_page, str) or next_page in pages:
                    raise ProviderError("La paginación de Drive no es válida; el listado está incompleto.")
                pages.add(next_page)
                page = next_page
        self._folder_id = root
        return files

    def get_bytes(self, item: dict) -> tuple[bytes, str, str]:
        root = self._root()
        if not isinstance(item, dict):
            raise DocumentError("Los metadatos del documento no son válidos.")
        current = self._metadata(_valid_id(item.get("id"), "archivo"))
        self._in_scope(current, root)
        for field in ("version", "modifiedTime", "name", "mimeType"):
            if field in item and item[field] != current.get(field):
                raise DocumentError("El documento cambió después del listado. Repite la sincronización.")
        mime = current["mimeType"]
        if mime not in SUPPORTED:
            raise DocumentError("Formato documental no admitido. Se admiten PDF, Word (.docx), TXT y Google Docs.")
        if "size" in current:
            try:
                size = int(current["size"])
                if size < 0:
                    raise ValueError()
            except (TypeError, ValueError):
                raise DocumentError("Drive devolvió un tamaño documental no válido.") from None
            if size > self.config.max_file_bytes:
                raise DocumentError("El documento supera el tamaño máximo permitido.")
        resource = self._service().files()
        if mime == DOC_MIME:
            request = resource.export_media(fileId=current["id"], mimeType="text/plain")
            filename, target_mime = current["name"] + ".txt", "text/plain"
        else:
            request = resource.get_media(fileId=current["id"], supportsAllDrives=True)
            filename, target_mime = current["name"], mime
        stream = _LimitedBytesIO(self.config.max_file_bytes)
        chunk_size = min(1024 * 1024, self.config.max_file_bytes + 1)
        downloader = MediaIoBaseDownload(stream, request, chunksize=chunk_size)
        try:
            for _ in range(self.config.max_file_bytes // chunk_size + 2):
                _, finished = downloader.next_chunk(num_retries=RETRIES)
                if finished:
                    break
            else:
                raise DocumentError("La descarga no terminó dentro del límite permitido.")
        except DocumentError:
            raise
        except Exception:
            raise ProviderError("No se pudo descargar el documento de Drive. Revisa conexión y permisos.") from None
        # Comprueba también movimientos y cambios ocurridos durante la descarga.
        after = self._metadata(current["id"])
        self._in_scope(after, root)
        if any(current.get(key) != after.get(key) for key in ("version", "modifiedTime", "mimeType", "name")):
            raise DocumentError("El documento cambió durante la descarga. Repite la sincronización.")
        return stream.getvalue(), filename, target_mime
