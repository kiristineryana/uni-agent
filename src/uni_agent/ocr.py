"""Local OCR with the OCR engine built into Windows 10/11 (no install, no cost, images never leave the PC).
`get_ocr()` returns a function bytes -> text, or None where Windows OCR isn't available."""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

log = logging.getLogger(__name__)

OcrFunc = Callable[[bytes], str]


def _windows_ocr() -> OcrFunc | None:
    try:
        from winrt.windows.graphics.imaging import BitmapDecoder
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream
    except ImportError:
        return None
    if OcrEngine.try_create_from_user_profile_languages() is None:
        log.warning("Windows OCR has no language installed; OCR disabled")
        return None

    async def recognize(data: bytes) -> str:
        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream)
        writer.write_bytes(data)
        await writer.store_async()
        await writer.flush_async()
        writer.detach_stream()
        stream.seek(0)
        bitmap = await (await BitmapDecoder.create_async(stream)).get_software_bitmap_async()
        if max(bitmap.pixel_width, bitmap.pixel_height) > OcrEngine.max_image_dimension:
            return ""
        result = await OcrEngine.try_create_from_user_profile_languages().recognize_async(bitmap)
        return "\n".join(line.text for line in result.lines)

    def ocr(data: bytes) -> str:
        try:
            return asyncio.run(recognize(data))  # called from worker threads, which have no event loop
        except Exception as e:  # unsupported format (EMF/WMF), corrupt image...
            log.info("OCR skipped an image (%s)", type(e).__name__)
            return ""

    return ocr


_cached: list = []


def get_ocr() -> OcrFunc | None:
    if not _cached:
        _cached.append(_windows_ocr())
    return _cached[0]
