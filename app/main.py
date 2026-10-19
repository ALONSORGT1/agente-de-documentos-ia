"""Asistente documental con ejecución local o consulta protegida en la nube."""
import logging
import secrets
import threading
import time
from collections import deque
from ipaddress import ip_address

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import ROOT, settings
from app.core.access import authenticated, create_session, COOKIE_NAME, SESSION_SECONDS
from app.core.errors import BusyError, ConfigurationError, DocumentError, ProviderError, StateError
from app.models.documento import AskRequest, AskResponse
from app.services.agent_service import AgentService
from app.services.sync_service import SyncService, _LOCK
from app.services import cloud_state

logger = logging.getLogger(__name__)
app = FastAPI(title="Agente de documentos IA", version="2.0.0", docs_url=None, redoc_url=None)
app.mount("/assets", StaticFiles(directory=ROOT / "assets"), name="assets")
_requests = deque()
_rate_lock = threading.Lock()
BODY_LIMIT = 32768


@app.middleware("http")
async def local_security(request: Request, call_next):
    try:
        loopback = ip_address(request.client.host).is_loopback if request.client else False
    except ValueError:
        loopback = False
    if not settings.is_cloud and (not loopback or request.url.hostname not in {"127.0.0.1", "localhost", "::1"}):
        return JSONResponse(status_code=403, content={"detail": "Servicio disponible solo desde localhost."})
    expected_origin = f"{request.url.scheme}://{request.url.netloc}"
    origin = request.headers.get("origin")
    public_page = settings.is_cloud and request.method == "GET" and (
        request.url.path == "/" or request.url.path.startswith("/assets/"))
    if not public_page and ((origin is not None and origin != expected_origin)
                            or request.headers.get("sec-fetch-site") == "cross-site"):
        return JSONResponse(status_code=403, content={"detail": "Origen de solicitud no autorizado."})
    if settings.is_cloud and request.url.path.rstrip("/") in {"/api/documents", "/api/ask", "/api/sync"}:
        if not settings.app_access_key:
            return JSONResponse(status_code=503, content={"detail": "El acceso todavía no está configurado en el servidor."})
        if not authenticated(request, settings.app_access_key):
            return JSONResponse(status_code=401, content={"detail": "Clave de acceso incorrecta o ausente."})
    if request.method == "POST":
        if request.headers.get("x-requested-with") != "AgenteDocumentalTIC":
            return JSONResponse(status_code=403, content={"detail": "Falta la cabecera X-Requested-With."})
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return JSONResponse(status_code=415, content={"detail": "Envía el cuerpo como application/json."})
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > BODY_LIMIT:
                return JSONResponse(status_code=413, content={"detail": "Solicitud demasiado grande (máximo 32 KiB)."})
            body.extend(chunk)
        request._body = bytes(body)
        now = time.monotonic()
        with _rate_lock:
            while _requests and _requests[0] <= now - 60:
                _requests.popleft()
            if len(_requests) >= settings.requests_per_minute:
                return JSONResponse(status_code=429, content={"detail": "Límite local de solicitudes. Espera un minuto."}, headers={"Retry-After": "60"})
            _requests.append(now)
    response = await call_next(request)
    response.headers.update({
        "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
        "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
    })
    return response


def local_only(request: Request):
    if settings.is_cloud:
        return
    key = settings.local_admin_key
    if key and not secrets.compare_digest(request.headers.get("X-Admin-Key", "").encode("utf-8"), key.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Clave administrativa incorrecta o ausente.")


@app.exception_handler(ConfigurationError)
@app.exception_handler(DocumentError)
async def bad_configuration(request, exc):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(StateError)
@app.exception_handler(BusyError)
async def state_error(request, exc):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(ProviderError)
async def provider_error(request, exc):
    return JSONResponse(status_code=502, content={"detail": str(exc)})


@app.get("/")
def index():
    return FileResponse(ROOT / "index.html")


@app.get("/api/health")
def health(request: Request):
    checks = settings.checks()
    configured = checks["openai"] and checks["drive_folder"] and (checks["google_credentials"] or checks["oauth_token"])
    if settings.is_cloud:
        checks.update({"access_key": bool(settings.app_access_key), "document_manifest": False,
                       "document_index": False, "documents": False, "vector_store": False})
        try:
            snapshot = SyncService.load_manifest()
        except StateError:
            pass
        else:
            checks.update({"document_manifest": True, "documents": bool(snapshot["documents"]),
                           "vector_store": bool(snapshot["vector_store_id"]),
                           "document_index": bool(snapshot["documents"] and snapshot["vector_store_id"])})
        configured = (checks["openai"] and bool(settings.app_access_key) and checks["drive_folder"]
                      and checks["document_manifest"] and checks["documents"] and checks["vector_store"])
    return {"status": "ok", "configured": configured,
            "checks": checks, "admin_key_required": bool(settings.local_admin_key),
            "deployment_mode": settings.deployment_mode, "access_key_required": settings.is_cloud,
            "authenticated": not settings.is_cloud or authenticated(request, settings.app_access_key),
            "sync_available": not settings.is_cloud or settings.cloud_sync_available,
            "sync_in_progress": _LOCK.locked(), "operation_in_progress": _LOCK.locked()}


@app.get("/api/documents")
def documents():
    doc = SyncService.load_manifest()
    # No entregar IDs internos de OpenAI, journals de recuperación ni operaciones pendientes.
    public = [{key: val for key, val in d.items() if key in {"name", "url", "drive_id", "mime_type", "modified_time"}}
              for d in doc.get("documents", {}).values()]
    return {"documents": public, "indexed": len(public), "last_sync": doc.get("last_sync"),
            "vector_store_configured": bool(doc.get("vector_store_id"))}


@app.post("/api/ask", response_model=AskResponse)
def ask(payload: AskRequest):
    # La misma exclusión protege una respuesta del reemplazo/baja de su evidencia.
    if not _LOCK.acquire(blocking=False):
        raise BusyError("Hay una consulta o sincronización en curso. Espera a que termine.")
    try:
        if not settings.openai_api_key:
            raise ConfigurationError("Falta OPENAI_API_KEY en .env. Reinicia el servidor tras configurarla.")
        with cloud_state.operation(settings):
            return AgentService().ask(payload.question, payload.use_search, payload.history)
    except (ConfigurationError, DocumentError, StateError, BusyError, ProviderError):
        raise
    except Exception as exc:
        logger.error("Consulta fallida (%s).", type(exc).__name__)
        raise ProviderError("Error al consultar OpenAI. Comprueba la configuración y vuelve a intentar.") from None
    finally:
        _LOCK.release()


@app.post("/api/sync", dependencies=[Depends(local_only)])
def sync():
    if settings.is_cloud and not settings.cloud_sync_available:
        raise StateError("La sincronización se realiza desde la instalación local. Actualiza DOCUMENT_MANIFEST_JSON en Vercel después de sincronizar.")
    try:
        if not settings.openai_api_key:
            raise ConfigurationError("Falta OPENAI_API_KEY en .env. Reinicia el servidor tras configurarla.")
        return SyncService().sync()
    except (ConfigurationError, DocumentError, StateError, BusyError, ProviderError):
        raise
    except Exception as exc:
        logger.error("Sincronización fallida (%s).", type(exc).__name__)
        raise ProviderError("Sincronización fallida. Comprueba OAuth, permisos y conexión con las APIs.") from None


@app.post("/api/session")
def start_session(request: Request):
    if not settings.is_cloud or not authenticated(request, settings.app_access_key):
        raise HTTPException(status_code=401, detail="Clave de acceso incorrecta o ausente.")
    response = JSONResponse({"authenticated": True})
    response.set_cookie(COOKIE_NAME, create_session(settings.app_access_key), max_age=SESSION_SECONDS,
                        secure=True, httponly=True, samesite="strict", path="/")
    return response


@app.post("/api/logout")
def end_session():
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(COOKIE_NAME, secure=True, httponly=True, samesite="strict", path="/")
    return response
