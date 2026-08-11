"""Meta Graph API helpers for Facebook Page + Instagram publishing."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

OAUTH_SCOPES = [
    'pages_show_list',
    'pages_read_engagement',
    'pages_manage_posts',
    'pages_manage_metadata',
    'instagram_basic',
    'instagram_content_publish',
    'business_management',
]


class MetaAPIError(Exception):
    """Raised when a Meta Graph API call fails."""


def friendly_user_error(exc: Exception) -> str:
    """Map technical publish/connect errors to short user-facing copy."""
    text = str(exc or '').strip()
    lower = text.lower()
    if not text:
        return 'Something went wrong. Please try again.'
    if '9007' in lower or 'media id is not available' in lower or 'not ready' in lower:
        return 'Your post is being published. It should appear shortly.'
    if 'public_base_url' in lower or ('https' in lower and 'image' in lower):
        return 'We could not publish this image right now. Please try again in a moment.'
    if 'not configured' in lower or 'app id' in lower or 'app secret' in lower:
        return 'This connection is not available right now. Please try again later.'
    if 'oauth' in lower or 'authorization' in lower or 'access token' in lower:
        return 'Please reconnect your account and try again.'
    if 'connect facebook' in lower:
        return 'Connect Facebook first, then try again.'
    if 'connect instagram' in lower:
        return 'Connect Instagram first, then try again.'
    if any(token in lower for token in ('code ', 'graph', 'uri', 'token', 'meta ', 'oauth')):
        return 'Something went wrong while publishing. Please try again.'
    return text


def meta_configured() -> bool:
    """True if Meta OAuth App ID and Secret are set (platform config)."""
    return bool(settings.META_APP_ID and settings.META_APP_SECRET)


def resolve_publish_credentials(user) -> dict:
    """
    Per-user OAuth connection only (Facebook Page token on UserProfile).
    Raises MetaAPIError if the user has not connected Meta.
    """
    profile = getattr(user, 'profile', None)
    if profile is not None and profile.meta_connected:
        return {
            'page_id': profile.facebook_page_id,
            'page_token': profile.facebook_page_access_token,
            'page_name': profile.facebook_page_name or 'Facebook Page',
            'ig_user_id': profile.instagram_business_account_id or '',
            'source': 'profile',
        }

    raise MetaAPIError(
        'Connect Facebook from Social Connections before publishing.'
    )


def facebook_permalink(post_id: str) -> str:
    """Best-effort public URL for a Graph photo/post id."""
    pid = (post_id or '').strip()
    if not pid:
        return ''
    return f'https://www.facebook.com/{pid}'


def instagram_permalink(media_id: str) -> str:
    """Instagram media ids are not always public URLs; link to Instagram home as fallback."""
    mid = (media_id or '').strip()
    if not mid:
        return ''
    return f'https://www.instagram.com/'


def facebook_publish_ready(user) -> bool:
    try:
        creds = resolve_publish_credentials(user)
    except MetaAPIError:
        return False
    return bool(creds.get('page_id') and creds.get('page_token'))


def instagram_publish_ready(user) -> bool:
    from .instagram_login import instagram_login_ready

    return instagram_login_ready(user)


def graph_base() -> str:
    version = (settings.META_GRAPH_VERSION or 'v21.0').lstrip('/')
    return f'https://graph.facebook.com/{version}'


def public_base_url() -> str:
    return (settings.PUBLIC_BASE_URL or '').rstrip('/')


def absolute_media_url(relative_or_url: str) -> str:
    """Build a public HTTPS URL for a media file (required by Instagram)."""
    if not relative_or_url:
        raise MetaAPIError('Post has no image to publish.')
    if relative_or_url.startswith('http://') or relative_or_url.startswith('https://'):
        return relative_or_url
    base = public_base_url()
    if not base:
        raise MetaAPIError(
            'PUBLIC_BASE_URL is not set. Instagram needs a public HTTPS image URL '
            '(e.g. your Cloudflare tunnel).'
        )
    path = relative_or_url if relative_or_url.startswith('/') else f'/{relative_or_url}'
    if not path.startswith('/media/'):
        media = settings.MEDIA_URL if settings.MEDIA_URL.startswith('/') else f'/{settings.MEDIA_URL}'
        path = f'{media.rstrip("/")}/{relative_or_url.lstrip("/")}'
    return f'{base}{path}'


def oauth_authorize_url(*, redirect_uri: str, state: str, rerequest: bool = False) -> str:
    if not meta_configured():
        raise MetaAPIError('Meta App ID/Secret are not configured.')
    params = {
        'client_id': settings.META_APP_ID,
        'redirect_uri': redirect_uri,
        'state': state,
        'scope': ','.join(OAUTH_SCOPES),
        'response_type': 'code',
    }
    if rerequest:
        # Force Meta to show permission dialog again (needed when IG scopes were skipped)
        params['auth_type'] = 'rerequest'
    version = (settings.META_GRAPH_VERSION or 'v21.0').lstrip('/')
    return f'https://www.facebook.com/{version}/dialog/oauth?{urlencode(params)}'


def _raise_for_graph(data: dict, *, fallback: str = 'Meta API error') -> None:
    err = data.get('error') if isinstance(data, dict) else None
    if err:
        message = err.get('error_user_msg') or err.get('message') or fallback
        code = err.get('code')
        raise MetaAPIError(f'{message}' + (f' (code {code})' if code is not None else ''))


def exchange_code_for_token(*, code: str, redirect_uri: str) -> str:
    """Exchange OAuth code for a short-lived user access token."""
    url = f'{graph_base()}/oauth/access_token'
    params = {
        'client_id': settings.META_APP_ID,
        'client_secret': settings.META_APP_SECRET,
        'redirect_uri': redirect_uri,
        'code': code,
    }
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, params=params)
        data = resp.json()
    _raise_for_graph(data, fallback='Failed to exchange OAuth code')
    token = data.get('access_token')
    if not token:
        raise MetaAPIError('No access token returned from Meta.')
    return token


def exchange_long_lived_user_token(short_token: str) -> str:
    url = f'{graph_base()}/oauth/access_token'
    params = {
        'grant_type': 'fb_exchange_token',
        'client_id': settings.META_APP_ID,
        'client_secret': settings.META_APP_SECRET,
        'fb_exchange_token': short_token,
    }
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, params=params)
        data = resp.json()
    _raise_for_graph(data, fallback='Failed to get long-lived token')
    token = data.get('access_token')
    if not token:
        raise MetaAPIError('No long-lived token returned from Meta.')
    return token


def fetch_facebook_user_id(user_token: str) -> str:
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(f'{graph_base()}/me', params={'access_token': user_token, 'fields': 'id'})
        data = resp.json()
    _raise_for_graph(data, fallback='Failed to fetch Facebook user')
    user_id = data.get('id')
    if not user_id:
        raise MetaAPIError('Could not read Facebook user id.')
    return str(user_id)


def fetch_managed_pages(user_token: str) -> list[dict]:
    """Return pages with id, name, access_token, and optional Instagram business account."""
    fields = (
        'id,name,access_token,'
        'instagram_business_account{id,username},'
        'connected_instagram_account{id,username}'
    )
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(
            f'{graph_base()}/me/accounts',
            params={'access_token': user_token, 'fields': fields, 'limit': 100},
        )
        data = resp.json()
    _raise_for_graph(data, fallback='Failed to list Facebook Pages')
    pages = list(data.get('data') or [])
    return enrich_pages_with_instagram(pages)


def _page_instagram_payload(data: dict) -> dict | None:
    """Normalize IG account object from Graph page fields."""
    if not isinstance(data, dict):
        return None
    ig = data.get('instagram_business_account') or data.get('connected_instagram_account')
    if isinstance(ig, dict) and ig.get('id'):
        return {
            'id': str(ig.get('id')),
            'username': str(ig.get('username') or ''),
        }
    return None


def fetch_page_instagram(*, page_id: str, page_token: str) -> dict | None:
    """
    Look up Instagram linked to a specific Page using the Page access token.
    More reliable than relying on /me/accounts nested fields alone.
    """
    if not page_id or not page_token:
        return None
    fields = 'instagram_business_account{id,username},connected_instagram_account{id,username}'
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(
            f'{graph_base()}/{page_id}',
            params={'fields': fields, 'access_token': page_token},
        )
        data = resp.json()
    if data.get('error'):
        logger.warning(
            'fetch_page_instagram failed for page %s: %s',
            page_id,
            data.get('error'),
        )
        return None
    return _page_instagram_payload(data)


def enrich_pages_with_instagram(pages: list[dict]) -> list[dict]:
    """Fill missing Instagram ids by querying each Page with its Page token."""
    enriched: list[dict] = []
    for page in pages:
        page = dict(page)
        existing = _page_instagram_payload(page)
        if existing:
            page['instagram_business_account'] = existing
            enriched.append(page)
            continue
        page_id = str(page.get('id') or '')
        page_token = str(page.get('access_token') or '')
        ig = fetch_page_instagram(page_id=page_id, page_token=page_token)
        if ig:
            page['instagram_business_account'] = ig
        enriched.append(page)
    return enriched


def pick_best_page(pages: list[dict], *, require_instagram: bool = False) -> dict:
    if not pages:
        raise MetaAPIError(
            'No Facebook Pages found. Create a Page and grant this app access, then reconnect.'
        )
    if require_instagram:
        for page in pages:
            ig = page.get('instagram_business_account') or {}
            if ig.get('id'):
                return page
        raise MetaAPIError(
            'No Facebook Page with a linked Instagram Business account was found. '
            'In Meta Business Suite, open your Page → Linked accounts → Instagram, '
            'convert IG to Business/Creator if needed, then Connect Instagram again '
            '(approve all Instagram permissions when Facebook asks).'
        )
    for page in pages:
        ig = page.get('instagram_business_account') or {}
        if ig.get('id'):
            return page
    return pages[0]


def pages_with_instagram(pages: list[dict]) -> list[dict]:
    return [p for p in pages if (p.get('instagram_business_account') or {}).get('id')]


def serialize_pages_for_session(pages: list[dict]) -> list[dict]:
    """Strip to JSON-safe fields for session page picker."""
    out = []
    for page in pages:
        ig = _page_instagram_payload(page) or (page.get('instagram_business_account') or {})
        out.append({
            'id': str(page.get('id') or ''),
            'name': str(page.get('name') or 'Facebook Page'),
            'access_token': str(page.get('access_token') or ''),
            'instagram_id': str(ig.get('id') or ''),
            'instagram_username': str(ig.get('username') or ''),
        })
    return out


def find_serialized_page(pages: list[dict], page_id: str) -> dict | None:
    for page in pages:
        if str(page.get('id') or '') == str(page_id):
            return page
    return None


def post_caption_text(post) -> str:
    caption = (post.caption or '').strip()
    description = (post.description or '').strip()
    if caption and description and caption != description:
        return f'{caption}\n\n{description}'
    return caption or description


def publish_facebook_photo(*, page_id: str, page_token: str, image_path: Path, caption: str) -> str:
    """Upload a photo to a Facebook Page. Returns the Graph post/photo id."""
    if not image_path.is_file():
        raise MetaAPIError('Image file not found for Facebook publish.')
    url = f'{graph_base()}/{page_id}/photos'
    with image_path.open('rb') as fh:
        files = {'source': (image_path.name, fh, 'image/png')}
        data = {'caption': caption, 'access_token': page_token, 'published': 'true'}
        with httpx.Client(timeout=120.0) as client:
            resp = client.post(url, data=data, files=files)
            result = resp.json()
    _raise_for_graph(result, fallback='Facebook photo publish failed')
    post_id = result.get('post_id') or result.get('id')
    if not post_id:
        raise MetaAPIError('Facebook publish succeeded but no post id was returned.')
    return str(post_id)


def _wait_for_ig_container(creation_id: str, page_token: str, *, attempts: int = 12) -> None:
    url = f'{graph_base()}/{creation_id}'
    with httpx.Client(timeout=30.0) as client:
        for _ in range(attempts):
            resp = client.get(
                url,
                params={'fields': 'status_code,status', 'access_token': page_token},
            )
            data = resp.json()
            _raise_for_graph(data, fallback='Instagram container status failed')
            status = (data.get('status_code') or '').upper()
            if status == 'FINISHED':
                return
            if status == 'ERROR':
                raise MetaAPIError(data.get('status') or 'Instagram media container failed.')
            time.sleep(2)
    raise MetaAPIError('Instagram media container timed out before it was ready.')


def publish_instagram_photo(
    *,
    ig_user_id: str,
    page_token: str,
    image_url: str,
    caption: str,
) -> str:
    """Create + publish an Instagram feed photo. Returns the published media id."""
    create_url = f'{graph_base()}/{ig_user_id}/media'
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(
            create_url,
            data={
                'image_url': image_url,
                'caption': caption,
                'access_token': page_token,
            },
        )
        created = resp.json()
    _raise_for_graph(created, fallback='Instagram media create failed')
    creation_id = created.get('id')
    if not creation_id:
        raise MetaAPIError('Instagram did not return a creation id.')

    _wait_for_ig_container(str(creation_id), page_token)

    publish_url = f'{graph_base()}/{ig_user_id}/media_publish'
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(
            publish_url,
            data={'creation_id': creation_id, 'access_token': page_token},
        )
        published = resp.json()
    _raise_for_graph(published, fallback='Instagram media publish failed')
    media_id = published.get('id')
    if not media_id:
        raise MetaAPIError('Instagram publish succeeded but no media id was returned.')
    return str(media_id)


def publish_post_to_meta(post, *, platforms: set[str] | None = None) -> dict:
    """
    Publish a post to Facebook and/or Instagram via Graph API.
    platforms: optional subset {'facebook', 'instagram'}; defaults to post flags.
    Credentials: that user's connected Facebook Page / Instagram from profile.
    Returns {'facebook': id|None, 'instagram': id|None}.
    """
    from django.utils import timezone

    want_fb = post.publish_to_facebook if platforms is None else 'facebook' in platforms
    want_ig = post.publish_to_instagram if platforms is None else 'instagram' in platforms

    if not want_fb and not want_ig:
        return {'facebook': None, 'instagram': None}

    if not post.image:
        raise MetaAPIError('An image is required to publish to Facebook or Instagram.')

    caption = post_caption_text(post)
    image_path = Path(post.image.path)
    errors: list[str] = []
    result = {'facebook': None, 'instagram': None}
    update_fields: list[str] = []

    if want_fb:
        try:
            creds = resolve_publish_credentials(post.user)
            fb_id = publish_facebook_photo(
                page_id=creds['page_id'],
                page_token=creds['page_token'],
                image_path=image_path,
                caption=caption,
            )
            result['facebook'] = fb_id
            post.facebook_post_id = fb_id
            post.facebook_published_at = timezone.now()
            post.publish_to_facebook = True
            update_fields.extend([
                'facebook_post_id',
                'facebook_published_at',
                'publish_to_facebook',
            ])
            logger.info(
                'Published post id=%s to Facebook page %s (via %s)',
                post.pk,
                creds['page_id'],
                creds.get('source'),
            )
        except MetaAPIError as exc:
            errors.append(f'Facebook: {exc}')
        except Exception as exc:
            logger.exception('Unexpected Facebook publish error for post id=%s', post.pk)
            errors.append(f'Facebook: {exc}')

    if want_ig:
        from .instagram_login import (
            instagram_publish_image_url,
            publish_instagram_login_photo,
            resolve_instagram_login_credentials,
        )

        try:
            ig_creds = resolve_instagram_login_credentials(post.user)
            image_url = instagram_publish_image_url(image_path, post.image.url)
            ig_id = publish_instagram_login_photo(
                ig_user_id=ig_creds['ig_user_id'],
                access_token=ig_creds['access_token'],
                image_url=image_url,
                caption=caption,
            )
            result['instagram'] = ig_id
            post.instagram_media_id = ig_id
            post.instagram_published_at = timezone.now()
            post.publish_to_instagram = True
            update_fields.extend([
                'instagram_media_id',
                'instagram_published_at',
                'publish_to_instagram',
            ])
            logger.info(
                'Published post id=%s to Instagram %s (via %s)',
                post.pk,
                ig_creds['ig_user_id'],
                ig_creds.get('source'),
            )
        except MetaAPIError as exc:
            from .instagram_login import InstagramStillProcessing, is_instagram_not_ready

            if isinstance(exc, InstagramStillProcessing) or is_instagram_not_ready(exc):
                logger.warning(
                    'Instagram still processing post id=%s; not showing 9007 to the user',
                    post.pk,
                )
            else:
                errors.append(f'Instagram: {exc}')
        except Exception as exc:
            logger.exception('Unexpected Instagram publish error for post id=%s', post.pk)
            errors.append(f'Instagram: {exc}')

    if update_fields:
        update_fields.append('updated_at')
        post.save(update_fields=list(dict.fromkeys(update_fields)))

    if errors:
        raise MetaAPIError(' · '.join(errors))
    return result
