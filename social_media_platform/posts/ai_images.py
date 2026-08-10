"""AI image generation for SocialFlow posts via OpenAI Images API only."""

from __future__ import annotations

import base64
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
from django.conf import settings
from django.core.files.base import ContentFile

logger = logging.getLogger(__name__)

OPENAI_IMAGES_URL = 'https://api.openai.com/v1/images/generations'
# Reject placeholders / corrupt tiny files (real gpt-image-1 outputs are ~1–3MB)
MIN_IMAGE_BYTES = 50_000
STATIC_AI_SAMPLES_DIR = Path(__file__).resolve().parent / 'static_ai_samples'
STATIC_AI_SAMPLE_NAMES = ('sample_01.jpg', 'sample_02.jpg', 'sample_03.jpg')


class ImageGenerationError(Exception):
    """Raised when AI image generation fails."""


def _static_image_files(count: int = 3) -> list[ContentFile]:
    """Return bundled professional sample images (testing without OpenAI)."""
    files: list[ContentFile] = []
    for name in STATIC_AI_SAMPLE_NAMES[:count]:
        path = STATIC_AI_SAMPLES_DIR / name
        if not path.is_file():
            raise ImageGenerationError(
                f'Static AI sample missing: {path.name}. '
                'Add files under posts/static_ai_samples/ or set USE_STATIC_AI_IMAGES=False.'
            )
        data = path.read_bytes()
        if len(data) < MIN_IMAGE_BYTES:
            raise ImageGenerationError(
                f'Static AI sample too small ({path.name}, {len(data)} bytes).'
            )
        ext = path.suffix.lower() or '.jpg'
        files.append(ContentFile(data, name=f'ai_static_{uuid.uuid4().hex}{ext}'))
    if len(files) < count:
        raise ImageGenerationError('Not enough static AI sample images configured.')
    logger.info('Using %s static sample image(s) (USE_STATIC_AI_IMAGES=True)', len(files))
    return files


def _api_key() -> str:
    key = (getattr(settings, 'OPENAI_API_KEY', '') or '').strip()
    if not key:
        raise ImageGenerationError('OPENAI_API_KEY is not set in .env.')
    return key


def _download_image_bytes(url: str) -> bytes:
    with httpx.Client(timeout=60.0) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.content


def _bytes_from_image_item(item: dict) -> bytes:
    b64 = item.get('b64_json')
    if b64:
        return base64.b64decode(b64)
    url = item.get('url')
    if url:
        return _download_image_bytes(url)
    raise ImageGenerationError('No image data returned from OpenAI.')


def _validate_png_bytes(image_bytes: bytes) -> bytes:
    if not image_bytes or len(image_bytes) < MIN_IMAGE_BYTES:
        raise ImageGenerationError(
            f'OpenAI returned an invalid image ({len(image_bytes or b"")} bytes). '
            'Refusing placeholders — try Generate again.'
        )
    # PNG magic or JPEG magic
    if image_bytes.startswith(b'\x89PNG\r\n\x1a\n') or image_bytes.startswith(b'\xff\xd8\xff'):
        return image_bytes
    # Some responses are raw; still accept if large enough
    if len(image_bytes) >= MIN_IMAGE_BYTES:
        return image_bytes
    raise ImageGenerationError('OpenAI returned unrecognized image data.')


def _openai_http_one(*, prompt: str, model: str, size: str, quality: str) -> bytes:
    payload: dict = {
        'model': model,
        'prompt': prompt,
        'n': 1,
        'size': size,
    }
    if quality:
        payload['quality'] = quality

    headers = {
        'Authorization': f'Bearer {_api_key()}',
        'Content-Type': 'application/json',
    }

    try:
        with httpx.Client(timeout=180.0) as client:
            resp = client.post(OPENAI_IMAGES_URL, headers=headers, json=payload)
            try:
                data = resp.json()
            except Exception:
                data = {}
    except httpx.HTTPError as exc:
        raise ImageGenerationError(f'OpenAI request failed: {exc}') from exc

    if resp.status_code >= 400:
        err = data.get('error') if isinstance(data, dict) else None
        message = (err or {}).get('message') if isinstance(err, dict) else None
        message = message or f'OpenAI error (HTTP {resp.status_code}).'

        if quality and 'quality' in message.lower():
            return _openai_http_one(prompt=prompt, model=model, size=size, quality='')

        if size != '1024x1024' and (
            'size' in message.lower() or 'invalid' in message.lower()
        ):
            return _openai_http_one(
                prompt=prompt, model=model, size='1024x1024', quality=quality
            )

        raise ImageGenerationError(message)

    items = data.get('data') if isinstance(data, dict) else None
    if not items:
        raise ImageGenerationError('No images returned from OpenAI.')

    return _validate_png_bytes(_bytes_from_image_item(items[0]))


def _openai_generate_one(*, prompt: str, model: str, size: str, quality: str) -> bytes:
    """Always use HTTP API (SDK often blocked on this Windows setup)."""
    return _openai_http_one(prompt=prompt, model=model, size=size, quality=quality)


def generate_image_files(prompt: str, count: int = 3) -> list[ContentFile]:
    """
    Generate exactly `count` OpenAI images.
    Raises if any image fails or is too small — never returns placeholders.
    When USE_STATIC_AI_IMAGES=True, returns bundled sample images (no API calls).
    """
    prompt = (prompt or '').strip()
    if not prompt:
        raise ImageGenerationError('Image prompt cannot be empty.')

    count = max(1, min(int(count), 4))

    if getattr(settings, 'USE_STATIC_AI_IMAGES', False):
        return _static_image_files(count=min(count, len(STATIC_AI_SAMPLE_NAMES)))

    model = getattr(settings, 'OPENAI_IMAGE_MODEL', 'gpt-image-1') or 'gpt-image-1'
    size = getattr(settings, 'OPENAI_IMAGE_SIZE', '1536x1024') or '1536x1024'
    quality = getattr(settings, 'OPENAI_IMAGE_QUALITY', 'medium') or 'medium'

    logger.info('Generating %s OpenAI image(s) model=%s size=%s', count, model, size)

    results: list[bytes | None] = [None] * count
    errors: list[str] = []

    def _job(index: int) -> tuple[int, bytes]:
        return index, _openai_generate_one(
            prompt=prompt,
            model=model,
            size=size,
            quality=quality,
        )

    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(_job, i) for i in range(count)]
        for fut in as_completed(futures):
            try:
                index, image_bytes = fut.result()
                results[index] = image_bytes
                logger.info('OpenAI image %s ready (%s bytes)', index + 1, len(image_bytes))
            except ImageGenerationError as exc:
                errors.append(str(exc))
                logger.warning('OpenAI image failed: %s', exc)
            except Exception as exc:
                errors.append(str(exc))
                logger.exception('Unexpected OpenAI image failure')

    if any(r is None for r in results):
        detail = errors[0] if errors else 'One or more images failed.'
        raise ImageGenerationError(
            f'Could not generate all {count} OpenAI images. {detail}'
        )

    files: list[ContentFile] = []
    for image_bytes in results:
        assert image_bytes is not None
        files.append(ContentFile(image_bytes, name=f'ai_{uuid.uuid4().hex}.png'))
    return files


def generate_image_file(prompt: str) -> ContentFile:
    """Generate a single OpenAI image."""
    return generate_image_files(prompt, count=1)[0]
