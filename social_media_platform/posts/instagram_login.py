"""Instagram API with Instagram Login (no Facebook Page required)."""

from __future__ import annotations

import logging
import time
import uuid
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode

import httpx
from django.conf import settings
from django.utils import timezone

from .meta import MetaAPIError, _raise_for_graph, absolute_media_url

logger = logging.getLogger(__name__)


class InstagramStillProcessing(MetaAPIError):
    """Transient 9007 — Instagram accepted the image but has not finished it yet."""

IG_OAUTH_SCOPES = (
    'instagram_business_basic',
    'instagram_business_content_publish',
)


def instagram_login_configured() -> bool:
    return bool(_ig_app_id() and _ig_app_secret())


def _ig_app_id() -> str:
    return (getattr(settings, 'INSTAGRAM_APP_ID', '') or '').strip()


def _ig_app_secret() -> str:
    return (getattr(settings, 'INSTAGRAM_APP_SECRET', '') or '').strip()


def ig_graph_base() -> str:
    version = (getattr(settings, 'INSTAGRAM_GRAPH_VERSION', '') or settings.META_GRAPH_VERSION or 'v21.0')
    version = str(version).lstrip('/')
    return f'https://graph.instagram.com/{version}'


def instagram_login_ready(user) -> bool:
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False
    token = (getattr(profile, 'instagram_access_token', '') or '').strip()
    user_id = (
        (getattr(profile, 'instagram_user_id', '') or '').strip()
        or (getattr(profile, 'instagram_business_account_id', '') or '').strip()
    )
    return bool(token and user_id)


def resolve_instagram_login_credentials(user) -> dict:
    profile = getattr(user, 'profile', None)
    if profile is None or not instagram_login_ready(user):
        raise MetaAPIError('Connect Instagram from Social Connections before publishing.')
    return {
        'ig_user_id': (
            (profile.instagram_user_id or '').strip()
            or (profile.instagram_business_account_id or '').strip()
        ),
        'access_token': (profile.instagram_access_token or '').strip(),
        'username': (profile.instagram_username or '').strip(),
        'source': 'instagram_login',
    }


def instagram_oauth_authorize_url(*, redirect_uri: str, state: str) -> str:
    if not instagram_login_configured():
        raise MetaAPIError(
            'Instagram Login is not configured. Set INSTAGRAM_APP_ID and INSTAGRAM_APP_SECRET.'
        )
    params = {
        'client_id': _ig_app_id(),
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': ','.join(IG_OAUTH_SCOPES),
        'state': state,
    }
    return f'https://www.instagram.com/oauth/authorize?{urlencode(params)}'


def _token_pair(data: dict) -> tuple[str, str]:
    if not isinstance(data, dict):
        return '', ''
    item = data
    nested = data.get('data')
    if isinstance(nested, list) and nested and isinstance(nested[0], dict):
        item = nested[0]
    token = (item.get('access_token') or '').strip()
    user_id = str(item.get('user_id') or item.get('id') or '').strip()
    return token, user_id


def exchange_instagram_code(*, code: str, redirect_uri: str) -> tuple[str, str]:
    """Exchange OAuth code for a short-lived Instagram user token."""
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(
            'https://api.instagram.com/oauth/access_token',
            data={
                'client_id': _ig_app_id(),
                'client_secret': _ig_app_secret(),
                'grant_type': 'authorization_code',
                'redirect_uri': redirect_uri,
                'code': code,
            },
        )
        try:
            data = resp.json()
        except Exception as exc:
            raise MetaAPIError('Instagram token exchange returned an invalid response.') from exc
    if resp.status_code >= 400:
        err = data.get('error_message') or data.get('error_description') if isinstance(data, dict) else None
        if not err and isinstance(data, dict):
            nested = data.get('error')
            if isinstance(nested, dict):
                err = nested.get('message')
            elif isinstance(nested, str):
                err = nested
        raise MetaAPIError(err or f'Instagram token exchange failed (HTTP {resp.status_code}).')
    token, user_id = _token_pair(data)
    if not token:
        raise MetaAPIError('Instagram did not return an access token.')
    return token, user_id


def exchange_long_lived_instagram_token(short_token: str) -> tuple[str, object | None]:
    """Return (token, expires_at). Falls back to the short-lived token if exchange fails."""
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(
            'https://graph.instagram.com/access_token',
            params={
                'grant_type': 'ig_exchange_token',
                'client_secret': _ig_app_secret(),
                'access_token': short_token,
            },
        )
        try:
            data = resp.json()
        except Exception:
            data = {}
    if resp.status_code >= 400 or (isinstance(data, dict) and data.get('error')):
        logger.warning('Instagram long-lived token exchange failed; using short-lived token')
        return short_token, timezone.now() + timedelta(hours=1)
    token = (data.get('access_token') or short_token).strip()
    expires_in = data.get('expires_in')
    expires_at = None
    try:
        if expires_in:
            expires_at = timezone.now() + timedelta(seconds=int(expires_in))
    except (TypeError, ValueError):
        expires_at = None
    return token, expires_at


def fetch_instagram_login_profile(access_token: str) -> dict:
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(
            f'{ig_graph_base()}/me',
            params={
                'fields': 'user_id,id,username,name,account_type',
                'access_token': access_token,
            },
        )
        data = resp.json()
    _raise_for_graph(data, fallback='Failed to fetch Instagram profile')
    user_id = str(data.get('user_id') or data.get('id') or '').strip()
    if not user_id:
        raise MetaAPIError('Could not read Instagram user id.')
    return {
        'user_id': user_id,
        'username': str(data.get('username') or ''),
        'name': str(data.get('name') or ''),
        'account_type': str(data.get('account_type') or ''),
    }


def _container_status(data: dict) -> str:
    return str(data.get('status_code') or data.get('status') or '').strip().upper()


def is_instagram_not_ready(exc: Exception) -> bool:
    text = str(exc or '').lower()
    return '9007' in text or 'media id is not available' in text or 'not ready for publishing' in text


def _wait_for_ig_login_container(creation_id: str, access_token: str, *, attempts: int = 30) -> None:
    """Poll until Instagram finishes processing. 9007 is treated as 'still waiting'."""
    url = f'{ig_graph_base()}/{creation_id}'
    time.sleep(5)
    with httpx.Client(timeout=30.0) as client:
        for attempt in range(attempts):
            resp = client.get(
                url,
                params={'fields': 'status_code,status', 'access_token': access_token},
            )
            data = resp.json()
            if isinstance(data, dict) and data.get('error'):
                err = data.get('error') or {}
                code = err.get('code')
                if code in {9007, 24}:
                    logger.info(
                        'Instagram container %s not ready yet (status error %s), waiting…',
                        creation_id,
                        code,
                    )
                    time.sleep(3)
                    continue
                _raise_for_graph(data, fallback='Instagram container status failed')
            status = _container_status(data)
            logger.info('Instagram container %s status=%s attempt=%s', creation_id, status or 'UNKNOWN', attempt + 1)
            if status == 'FINISHED':
                time.sleep(3)
                return
            if status in {'ERROR', 'EXPIRED'}:
                detail = data.get('status') or status
                raise MetaAPIError(
                    f'Instagram could not process the image ({detail}). '
                    'Use a public HTTPS image URL and a JPEG/PNG between 4:5 and 1.91:1.'
                )
            time.sleep(3)
    # Still processing — publish step will keep retrying 9007 instead of erroring here.


def _publish_container(*, ig_user_id: str, access_token: str, creation_id: str) -> dict:
    publish_url = f'{ig_graph_base()}/{ig_user_id}/media_publish'
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(
            publish_url,
            data={'creation_id': creation_id, 'access_token': access_token},
        )
        return resp.json()


def publish_instagram_login_photo(*, ig_user_id: str, access_token: str, image_url: str, caption: str) -> str:
    """Create + publish a feed photo via graph.instagram.com. Returns media id."""
    create_url = f'{ig_graph_base()}/{ig_user_id}/media'
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(
            create_url,
            data={
                'image_url': image_url,
                'caption': caption,
                'access_token': access_token,
            },
        )
        created = resp.json()
    _raise_for_graph(created, fallback='Instagram media create failed')
    creation_id = created.get('id')
    if not creation_id:
        raise MetaAPIError('Instagram did not return a creation id.')

    _wait_for_ig_login_container(str(creation_id), access_token)

    published = {}
    for attempt in range(20):
        published = _publish_container(
            ig_user_id=ig_user_id,
            access_token=access_token,
            creation_id=str(creation_id),
        )
        err = published.get('error') if isinstance(published, dict) else None
        if err and err.get('code') == 9007:
            logger.info('Instagram still processing (9007) attempt %s — waiting silently', attempt + 1)
            time.sleep(4)
            continue
        break

    err = published.get('error') if isinstance(published, dict) else None
    if err and err.get('code') == 9007:
        # Do not surface 9007 to the user — Instagram usually finishes a few seconds later.
        logger.warning('Instagram container %s still processing after retries; skipping user error', creation_id)
        raise InstagramStillProcessing('instagram_still_processing')

    _raise_for_graph(published, fallback='Instagram media publish failed')
    media_id = published.get('id')
    if not media_id:
        raise MetaAPIError('Instagram publish succeeded but no media id was returned.')
    return str(media_id)


# Instagram feed photos must be between 4:5 (portrait) and 1.91:1 (landscape).
_IG_MIN_ASPECT = 4 / 5
_IG_MAX_ASPECT = 1.91


def _fit_instagram_aspect(img):
    """Center-crop to a ratio Instagram accepts for feed posts."""
    width, height = img.size
    if width < 1 or height < 1:
        return img
    ratio = width / height
    if _IG_MIN_ASPECT <= ratio <= _IG_MAX_ASPECT:
        return img
    if ratio < _IG_MIN_ASPECT:
        # Too tall — crop height to 4:5
        new_h = max(1, int(round(width / _IG_MIN_ASPECT)))
        top = max(0, (height - new_h) // 2)
        return img.crop((0, top, width, top + min(new_h, height - top)))
    # Too wide — crop width to 1.91:1
    new_w = max(1, int(round(height * _IG_MAX_ASPECT)))
    left = max(0, (width - new_w) // 2)
    return img.crop((left, 0, left + min(new_w, width - left), height))


def prepare_instagram_image_file(image_path: Path) -> Path | None:
    """
    Return a JPEG path Instagram can publish (valid aspect ratio + RGB JPEG).
    Always writes under media/posts/ig_ready/.
    """
    source = Path(image_path) if image_path else None
    if not source or not source.is_file():
        return None
    try:
        from PIL import Image, ImageOps

        out_dir = Path(settings.MEDIA_ROOT) / 'posts' / 'ig_ready'
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f'{uuid.uuid4().hex}.jpg'
        with Image.open(source) as img:
            img = ImageOps.exif_transpose(img)
            rgb = img.convert('RGB')
            fitted = _fit_instagram_aspect(rgb)
            # Keep a reasonable publish size without making tiny images
            max_side = 1920
            w, h = fitted.size
            if max(w, h) > max_side:
                scale = max_side / float(max(w, h))
                fitted = fitted.resize(
                    (max(1, int(w * scale)), max(1, int(h * scale))),
                    Image.Resampling.LANCZOS,
                )
            fitted.save(out_path, format='JPEG', quality=92, optimize=True)
        logger.info(
            'Prepared Instagram JPEG %s from %s',
            out_path.name,
            source.name,
        )
        return out_path
    except Exception:
        logger.exception('Could not prepare image for Instagram: %s', source)
        return None


def public_image_url_for_instagram(image_url: str) -> str:
    url = absolute_media_url(image_url)
    if (
        url.startswith('http://localhost')
        or url.startswith('http://127.0.0.1')
        or not url.startswith('https://')
    ):
        raise MetaAPIError(
            'Instagram needs a public HTTPS image URL. '
            'Set PUBLIC_BASE_URL to a public https tunnel/domain Meta can reach.'
        )
    return url


def instagram_publish_image_url(image_path: Path, image_url: str) -> str:
    """
    Instagram fetches the image itself. Always publish a prepared JPEG with a
    valid feed aspect ratio so Meta does not reject unusual AI/upload sizes.
    """
    source = Path(image_path) if image_path else None
    publish_rel = image_url
    prepared = prepare_instagram_image_file(source) if source else None
    if prepared and prepared.is_file():
        publish_rel = f'{settings.MEDIA_URL.rstrip("/")}/posts/ig_ready/{prepared.name}'
    return public_image_url_for_instagram(publish_rel)


def public_video_url_for_instagram(video_url: str) -> str:
    url = absolute_media_url(video_url)
    if (
        url.startswith('http://localhost')
        or url.startswith('http://127.0.0.1')
        or not url.startswith('https://')
    ):
        raise MetaAPIError(
            'Instagram needs a public HTTPS video URL. '
            'Set PUBLIC_BASE_URL to a public https tunnel/domain Meta can reach.'
        )
    return url


def publish_instagram_login_carousel(
    *,
    ig_user_id: str,
    access_token: str,
    image_urls: list[str],
    caption: str,
) -> str:
    """Publish an Instagram multi-photo post (2–10 images)."""
    if len(image_urls) < 2:
        raise MetaAPIError('Instagram multi-photo post needs at least 2 images.')
    if len(image_urls) > 10:
        image_urls = image_urls[:10]

    child_ids: list[str] = []
    create_url = f'{ig_graph_base()}/{ig_user_id}/media'
    with httpx.Client(timeout=60.0) as client:
        for image_url in image_urls:
            resp = client.post(
                create_url,
                data={
                    'image_url': image_url,
                    'is_carousel_item': 'true',
                    'access_token': access_token,
                },
            )
            created = resp.json()
            _raise_for_graph(created, fallback='Instagram multi-photo item create failed')
            cid = created.get('id')
            if not cid:
                raise MetaAPIError('Instagram did not return a photo item id.')
            child_ids.append(str(cid))
            # Meta requires each child container to finish before creating the parent.
            _wait_for_ig_login_container(str(cid), access_token, attempts=24)

    children = ','.join(child_ids)
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(
            create_url,
            data={
                'media_type': 'CAROUSEL',
                'children': children,
                'caption': caption,
                'access_token': access_token,
            },
        )
        parent = resp.json()
    _raise_for_graph(parent, fallback='Instagram multi-photo create failed')
    creation_id = parent.get('id')
    if not creation_id:
        raise MetaAPIError('Instagram did not return a multi-photo creation id.')

    _wait_for_ig_login_container(str(creation_id), access_token, attempts=24)

    published = {}
    for attempt in range(20):
        published = _publish_container(
            ig_user_id=ig_user_id,
            access_token=access_token,
            creation_id=str(creation_id),
        )
        err = published.get('error') if isinstance(published, dict) else None
        if err and err.get('code') == 9007:
            logger.info('Instagram multi-photo still processing (9007) attempt %s', attempt + 1)
            time.sleep(4)
            continue
        break

    err = published.get('error') if isinstance(published, dict) else None
    if err and err.get('code') == 9007:
        raise InstagramStillProcessing('instagram_still_processing')
    _raise_for_graph(published, fallback='Instagram multi-photo publish failed')
    media_id = published.get('id')
    if not media_id:
        raise MetaAPIError('Instagram multi-photo publish succeeded but no media id was returned.')
    return str(media_id)


def publish_instagram_login_video(
    *,
    ig_user_id: str,
    access_token: str,
    video_url: str,
    caption: str,
) -> str:
    """Publish an Instagram feed video (Reels-compatible container)."""
    create_url = f'{ig_graph_base()}/{ig_user_id}/media'
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(
            create_url,
            data={
                'media_type': 'REELS',
                'video_url': video_url,
                'caption': caption,
                'share_to_feed': 'true',
                'access_token': access_token,
            },
        )
        created = resp.json()
    _raise_for_graph(created, fallback='Instagram video create failed')
    creation_id = created.get('id')
    if not creation_id:
        raise MetaAPIError('Instagram did not return a video creation id.')

    _wait_for_ig_login_container(str(creation_id), access_token, attempts=40)

    published = {}
    for attempt in range(30):
        published = _publish_container(
            ig_user_id=ig_user_id,
            access_token=access_token,
            creation_id=str(creation_id),
        )
        err = published.get('error') if isinstance(published, dict) else None
        if err and err.get('code') == 9007:
            logger.info('Instagram video still processing (9007) attempt %s', attempt + 1)
            time.sleep(5)
            continue
        break

    err = published.get('error') if isinstance(published, dict) else None
    if err and err.get('code') == 9007:
        raise InstagramStillProcessing('instagram_still_processing')

    _raise_for_graph(published, fallback='Instagram video publish failed')
    media_id = published.get('id')
    if not media_id:
        raise MetaAPIError('Instagram video publish succeeded but no media id was returned.')
    return str(media_id)
