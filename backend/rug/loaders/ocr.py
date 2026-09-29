import io
import logging

import pytesseract
from PIL import Image, UnidentifiedImageError

log = logging.getLogger(__name__)


class ImageSkipped(Exception):
    pass


def ocr_image(blob: bytes, min_px: int) -> str:
    """OCR an embedded image. Raises ImageSkipped for tiny or unreadable images
    (icons, logos, EMF/WMF vector formats Pillow can't rasterise)."""
    try:
        img: Image.Image = Image.open(io.BytesIO(blob))
        img.load()
    except (UnidentifiedImageError, OSError) as e:
        raise ImageSkipped(f"unreadable image: {e}") from e
    if img.width < min_px or img.height < min_px:
        raise ImageSkipped(f"too small ({img.width}x{img.height})")
    if img.mode not in ("L", "RGB"):
        img = img.convert("RGB")
    text: str = pytesseract.image_to_string(img, lang="eng")
    return " ".join(text.split())
