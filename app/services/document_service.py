"""Validate supported documents before transferring them to the search provider."""
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import PurePath
import re

from app.core.errors import DocumentError


@dataclass(frozen=True)
class PreparedDocument:
    content: bytes
    filename: str
    content_sha256: str
    text_characters: int


def prepare_document(raw: bytes, filename: str, mime_type: str, max_bytes: int) -> PreparedDocument:
    """Reject binary text and PDFs that require OCR; never send them blindly."""
    if not raw:
        raise DocumentError("El archivo está vacío.")
    if len(raw) > max_bytes:
        raise DocumentError("El archivo supera el tamaño máximo configurado.")
    suffix = ".txt"
    if mime_type == "application/pdf":
        from pypdf import PdfReader

        try:
            reader = PdfReader(BytesIO(raw))
            if reader.is_encrypted and not reader.decrypt(""):
                raise DocumentError("El PDF está protegido con contraseña.")
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except DocumentError:
            raise
        except Exception as exc:
            raise DocumentError("El PDF no se puede leer o está dañado.") from exc
        if not text.strip():
            raise DocumentError("El PDF no contiene texto extraíble; necesita OCR.")
        content, suffix = raw, ".pdf"
    elif mime_type == "text/plain":
        encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        try:
            text = raw.decode(encoding)
        except UnicodeError as exc:
            raise DocumentError("El TXT debe estar codificado como UTF-8 o UTF-16 con BOM.") from exc
        if any(ord(char) < 32 and char not in "\t\r\n\f" for char in text):
            raise DocumentError("El TXT contiene datos binarios o caracteres de control.")
        if not text.strip():
            raise DocumentError("El documento no contiene texto utilizable.")
        content = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    else:
        raise DocumentError("Formato de descarga no admitido.")
    if len(content) > max_bytes:
        raise DocumentError("El texto normalizado supera el tamaño máximo configurado.")
    # Keep the actual Drive name in the manifest; only sanitize the upload name.
    name = PurePath(filename.replace("\\", "/")).name
    stem = re.sub(r"[^\w .-]", "_", name.rsplit(".", 1)[0]).strip(" .")[:100] or "documento"
    return PreparedDocument(content, stem + suffix, sha256(content).hexdigest(), len(text.strip()))
