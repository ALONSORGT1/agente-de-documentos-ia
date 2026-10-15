"""Prepare private deployment settings from a finished local synchronization."""
import argparse
import json
import os
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["DEPLOYMENT_MODE"] = "local"

from app.core.config import settings
from app.services.sync_service import SyncService


def prepare_cloud(rotate_access_key=False):
    manifest = SyncService.load_manifest()
    if not manifest.get("vector_store_id") or not manifest.get("documents"):
        raise ValueError("Primero sincroniza una carpeta con documentos desde la aplicación local.")
    if any(manifest.get(key) for key in ("pending", "cleanup", "update_intents", "store_creation_pending")):
        raise ValueError("Termina o reconcilia la sincronización local antes de publicar el índice.")
    snapshot = dict(manifest)
    snapshot["managed_file_ids"] = sorted({doc["openai_file_id"] for doc in manifest["documents"].values()})
    SyncService._validate_manifest(snapshot)
    snapshot_json = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(snapshot_json.encode("utf-8")) > 48000:
        raise ValueError("El índice supera el tamaño previsto para esta publicación. Usa almacenamiento externo.")
    destination = ROOT / ".deployment"
    destination.mkdir(exist_ok=True)
    key_file = destination / "access-key.txt"
    if rotate_access_key or not key_file.exists():
        key_file.write_text(secrets.token_urlsafe(24), encoding="utf-8")
    access_key = key_file.read_text(encoding="utf-8").strip()
    if len(access_key) < 24:
        raise ValueError("La clave de acceso debe tener al menos 24 caracteres.")
    config = {
        "DEPLOYMENT_MODE": "cloud",
        "APP_ACCESS_KEY": access_key,
        "DOCUMENT_MANIFEST_JSON": snapshot_json,
        "GOOGLE_DRIVE_FOLDER_ID": settings.drive_folder_id,
        "DRIVE_INCLUDE_SUBFOLDERS": str(settings.drive_include_subfolders).lower(),
        "OPENAI_MODEL": settings.openai_model,
    }
    (destination / "manifest.json").write_text(snapshot_json, encoding="utf-8")
    (destination / "vercel-env.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Preparados {len(snapshot['documents'])} documentos para consulta en línea.")
    print("Configuración privada: .deployment/vercel-env.json")
    print("Clave de acceso: .deployment/access-key.txt (no subir a GitHub)")
    return config


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rotate-access-key", action="store_true", help="Generar otra clave; requiere actualizar Vercel.")
    args = parser.parse_args()
    try:
        prepare_cloud(args.rotate_access_key)
    except ValueError as exc:
        parser.exit(1, f"No se preparó la publicación: {exc}\n")
