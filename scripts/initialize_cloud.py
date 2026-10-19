"""Seed a NEW private Blob store once; never replace an existing cloud journal."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from app.core.errors import BusyError, StateError


def initialize(env_file, manifest_file):
    load_dotenv(env_file, override=False)
    from app.core.config import Settings
    from app.services.cloud_state import CloudState
    from app.services import sync_service

    config = Settings()
    sync_service.settings = config
    manifest = json.loads(Path(manifest_file).read_text(encoding='utf-8-sig'))
    sync_service.SyncService._validate_manifest(manifest)
    if any(manifest.get(key) for key in ('pending', 'cleanup', 'update_intents', 'store_creation_pending')):
        raise StateError('Completa la sincronización local antes de iniciar el estado de la nube.')
    CloudState(config).initialize(manifest)
    print(f"Base privada iniciada con {len(manifest['documents'])} documentos. No se volvieron a indexar.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env.local')
    parser.add_argument('--manifest', type=Path, default=ROOT / '.deployment' / 'manifest.json')
    args = parser.parse_args()
    try:
        initialize(args.env_file, args.manifest)
    except BusyError:
        parser.exit(1, 'El almacenamiento ya contiene una base. Se conservó sin sobrescribir.\n')
    except (OSError, ValueError, StateError):
        parser.exit(1, 'No se inició la base. Revisa la configuración privada y el manifiesto local.\n')
