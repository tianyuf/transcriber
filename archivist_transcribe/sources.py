"""Where pages come from: a PDF, a single image, or a folder of images (one image per page).

Archive visits often produce phone photos rather than PDFs, so a folder of JPEGs is a
first-class source: its images are pages, in natural filename order (IMG_2 before IMG_10).
"""
from __future__ import annotations

import base64
import math
import re
from pathlib import Path

import pymupdf

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp", ".gif", ".jp2"}
MAX_SIDE = 2400  # photos are downscaled to this many pixels on their long side before upload
IMAGE_PATCH = 28  # vision models bill roughly one input token per 28x28 pixels


def natural_key(p: Path):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.name)]


class Source:
    def __init__(self, path: Path):
        self.path = path
        if path.is_dir():
            self.kind = "images"
            self.files = sorted((f for f in path.iterdir() if f.suffix.lower() in IMAGE_EXTS and not f.name.startswith(".")),
                                key=natural_key)
            if not self.files:
                raise SystemExit(f"no images found in {path} (looked for {', '.join(sorted(IMAGE_EXTS))})")
        elif path.suffix.lower() in IMAGE_EXTS:
            self.kind = "images"
            self.files = [path]
        elif path.suffix.lower() == ".pdf":
            self.kind = "pdf"
            self.files = []
        else:
            raise SystemExit(f"not a PDF, an image or a folder of images: {path}")
        if not path.exists():
            raise SystemExit(f"not found: {path}")

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def stem(self) -> str:
        return self.path.name if self.path.is_dir() else self.path.stem

    def page_count(self) -> int:
        if self.kind == "pdf":
            with pymupdf.open(self.path) as doc:
                return doc.page_count
        return len(self.files)

    def label(self, index: int) -> str:
        """How to name a page to the user: a page number, or the photo's filename."""
        return f"page {index + 1}" if self.kind == "pdf" else f"page {index + 1} ({self.files[index].name})"

    def _pixmap(self, index: int, dpi: int, max_side: int) -> pymupdf.Pixmap:
        if self.kind == "pdf":
            with pymupdf.open(self.path) as doc:
                return doc[index].get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
        pix = pymupdf.Pixmap(str(self.files[index]))
        if pix.alpha or (pix.colorspace and pix.colorspace.n != 3):
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)  # drop alpha, convert grey/CMYK
            if pix.alpha:
                pix = pymupdf.Pixmap(pix, 0)
        side = max(pix.width, pix.height)
        if side > max_side:
            f = max_side / side
            pix = pymupdf.Pixmap(pix, max(1, int(pix.width * f)), max(1, int(pix.height * f)))
        return pix

    def render(self, index: int, dpi: int) -> tuple[bytes, str]:
        """Page image for the model: (bytes, mime type). PDFs as PNG, photos as JPEG."""
        pix = self._pixmap(index, dpi, MAX_SIDE)
        if self.kind == "pdf":
            return pix.tobytes("png"), "image/png"
        return pix.tobytes("jpeg", jpg_quality=90), "image/jpeg"

    def preview(self, index: int, dpi: int = 110, max_side: int = 1600) -> str:
        """A smaller JPEG data: URL for the review pages."""
        pix = self._pixmap(index, dpi, max_side)
        return "data:image/jpeg;base64," + base64.b64encode(pix.tobytes("jpeg", jpg_quality=72)).decode()

    def image_tokens(self, index: int, dpi: int) -> int:
        """Approximate input tokens for this page's image, without rendering it."""
        if self.kind == "pdf":
            with pymupdf.open(self.path) as doc:
                r = doc[index].rect
            w, h = r.width * dpi / 72, r.height * dpi / 72
            if max(w, h) > MAX_SIDE:
                f = MAX_SIDE / max(w, h)
                w, h = w * f, h * f
        else:
            with pymupdf.open(str(self.files[index])) as doc:
                r = doc[0].rect  # image documents report their pixel size
            w, h = r.width, r.height
            if max(w, h) > MAX_SIDE:
                f = MAX_SIDE / max(w, h)
                w, h = w * f, h * f
        return math.ceil(w / IMAGE_PATCH) * math.ceil(h / IMAGE_PATCH)

    def text_layer(self, index: int) -> str:
        """Embedded text for a PDF page ('' for photos or image-only scans)."""
        if self.kind != "pdf":
            return ""
        with pymupdf.open(self.path) as doc:
            return doc[index].get_text("text")
