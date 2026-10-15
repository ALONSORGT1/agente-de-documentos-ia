"""Temporales aislados dentro del proyecto, también en Windows sandbox."""
from pathlib import Path
from uuid import uuid4


def pytest_configure(config):
    if not config.option.basetemp:
        # Cada ejecución usa una ruta nueva: pytest no borra temporales anteriores.
        root = Path(__file__).resolve().parents[1]
        config.option.basetemp = str(root / ".test-tmp" / uuid4().hex)
