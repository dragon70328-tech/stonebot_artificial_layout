"""PDF preflight checks for production vector intake."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PdfPreflight:
    source_path: str
    sha256: str
    file_size: int
    page_count: int
    page_sizes: tuple[tuple[float, float], ...]
    producer: str
    vector_pages: int
    text_pages: int
    raster_images: int
    encrypted: bool

    @property
    def is_vector(self) -> bool:
        return self.vector_pages == self.page_count and self.vector_pages > 0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_pdf(path: str | Path) -> PdfPreflight:
    """Validate a local PDF and summarize whether it is suitable for intake."""

    import pymupdf

    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"PDF 不存在：{source}")
    if source.suffix.lower() != ".pdf":
        raise ValueError(f"仅接受 PDF：{source}")

    with pymupdf.open(str(source)) as doc:
        vector_pages = 0
        text_pages = 0
        raster_images = 0
        page_sizes: list[tuple[float, float]] = []
        for page in doc:
            page_sizes.append((float(page.rect.width), float(page.rect.height)))
            drawings = page.get_drawings()
            if drawings:
                vector_pages += 1
            if page.get_text("words"):
                text_pages += 1
            raster_images += len(page.get_images(full=True))

        metadata = doc.metadata or {}
        return PdfPreflight(
            source_path=str(source),
            sha256=_sha256(source),
            file_size=source.stat().st_size,
            page_count=doc.page_count,
            page_sizes=tuple(page_sizes),
            producer=str(metadata.get("producer") or ""),
            vector_pages=vector_pages,
            text_pages=text_pages,
            raster_images=raster_images,
            encrypted=bool(doc.is_encrypted),
        )
