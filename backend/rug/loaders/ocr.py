import io
import logging

import pytesseract
from PIL import Image, UnidentifiedImageError

from rug.loaders.base import LoaderEnvironmentError

log = logging.getLogger(__name__)


class ImageSkipped(Exception):
    """This image is deliberately not OCR'd (tiny, unreadable or over budget)."""


def ocr_image(blob: bytes, min_px: int, max_pixels: int, timeout_s: int) -> str:
    """OCR an embedded image.

    Raises ImageSkipped for tiny, unreadable (EMF/WMF), absurdly large or timed-out images,
    and LoaderEnvironmentError when Tesseract itself is unavailable.
    """
    try:
        img: Image.Image = Image.open(io.BytesIO(blob))  # lazy: size comes from the header
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
        raise ImageSkipped(f"unreadable image: {e}") from e
    if img.width < min_px or img.height < min_px:
        raise ImageSkipped(f"too small ({img.width}x{img.height})")
    if img.width * img.height > 16 * max_pixels:
        raise ImageSkipped(f"too large to decode ({img.width}x{img.height})")
    try:
        img.load()
    except (OSError, Image.DecompressionBombError) as e:
        raise ImageSkipped(f"undecodable image: {e}") from e
    if img.width * img.height > max_pixels:
        scale = (max_pixels / (img.width * img.height)) ** 0.5
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))))
    if img.mode not in ("L", "RGB"):
        img = img.convert("RGB")
    try:
        text: str = pytesseract.image_to_string(img, lang="eng", timeout=timeout_s)
    except pytesseract.TesseractNotFoundError as e:
        raise LoaderEnvironmentError(f"Tesseract is not installed: {e}") from e
    except pytesseract.TesseractError as e:  # bad input image; a RuntimeError subclass
        raise ImageSkipped(f"Tesseract failed: {e}") from e
    except RuntimeError as e:  # pytesseract signals its timeout with a bare RuntimeError
        raise ImageSkipped(f"OCR timed out after {timeout_s}s") from e
    return " ".join(text.split())
