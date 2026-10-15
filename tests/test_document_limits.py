import pytest

from app.core.errors import DocumentError
from app.services.document_service import prepare_document


def test_normalized_text_cannot_exceed_upload_limit():
    raw = ("漢" * 510).encode("utf-16")
    assert len(raw) <= 1024
    with pytest.raises(DocumentError, match="normalizado"):
        prepare_document(raw, "texto.txt", "text/plain", 1024)
