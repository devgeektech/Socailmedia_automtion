"""Meta Graph API helpers for Facebook Page + Instagram publishing."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
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
    """Map technical publish/connect errors to short English user-facing copy.

    Never surface Meta's localized/raw Graph messages to the UI.
    """
    import re

    text = str(exc or '').strip()
    if not text:
        return 'Something went wrong. Please try again.'

    cleaned = re.sub(r'\s*\(code\s*\d+\)\s*$', '', text, flags=re.IGNORECASE).strip()
    # Strip platform prefixes for matching
    bare = re.sub(r'^(facebook|instagram)\s*:\s*', '', cleaned, flags=re.IGNORECASE).strip()
    lower = f'{cleaned} {bare}'.lower()
    # Non-Latin scripts (e.g. Punjabi/Gurmukhi Meta locale messages)
    has_non_latin = bool(re.search(r'[^\x00-\x7F]', cleaned))

    if '9007' in lower or 'media id is not available' in lower or 'not ready' in lower:
        return 'Your post is being published. It should appear shortly.'
    if 'aspect ratio' in lower or 'aspect_ratio' in lower or 'ਆਕਾਰ ਅਨੁਪਾਤ' in cleaned:
        return (
            'Instagram rejected a photo because of its shape. '
            'Try publishing again - photos are now auto-adjusted to fit Instagram.'
        )
    if (
        'public_base_url' in lower
        or 'public https' in lower
        or ('https' in lower and 'instagram' in lower and ('image' in lower or 'video' in lower or 'photo' in lower))
    ):
        return (
            'Instagram needs a public https link to your photos. '
            'Set PUBLIC_BASE_URL to a public https address (for example an ngrok tunnel), then try again.'
        )
    if 'image file not found' in lower or 'file not found' in lower:
        return 'A photo file is missing. Please upload or select your photos again, then retry.'
    if 'carousel needs' in lower or 'multi-photo post needs' in lower or 'needs at least 2' in lower:
        return 'Select at least 2 photos to post them together.'
    if any(x in lower for x in ('session has expired', 'invalid oauth', 'error validating access token', 'code 190', '(#190)')):
        return 'Your connection expired. Reconnect Facebook/Instagram, then try again.'
    if any(x in lower for x in ('permission', '(#200)', 'code 200', 'not authorized', 'lacks permission')):
        return 'Missing permission to post. Reconnect your account and allow posting permissions.'
    if 'not configured' in lower or 'app id' in lower or 'app secret' in lower:
        return 'This connection is not available right now. Please try again later.'
    if 'connect facebook' in lower:
        return 'Connect Facebook first, then try again.'
    if 'connect instagram' in lower:
        return 'Connect Instagram first, then try again.'
    if 'could not process the image' in lower or 'failed to download' in lower:
        return 'Instagram could not process one of the photos. Please try again with a different photo.'

    # Never show Meta locale/raw strings (non-English Graph copy)
    if has_non_latin:
        return 'Instagram could not publish this photo. Please try again with a different photo.'

    if bare and len(bare) <= 180 and bare.isascii() and not any(
        token in lower for token in ('redirect_uri', 'client_secret', 'appsecret', 'exchange code', 'oauth')
    ):
        # Only allow short plain English app messages we raised ourselves
        if any(bare.lower().startswith(p) for p in (
            'connect ', 'select ', 'an image', 'a photo', 'a video', 'pick ', 'facebook ', 'instagram ',
            'missing ', 'your connection', 'this connection', 'the photo', 'a photo file',
        )):
            return bare

    return 'Something went wrong while publishing. Please try again.'


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


def publish_facebook_photo(
    *,
    page_id: str,
    page_token: str,
    image_path: Path,
    caption: str,
    cancel_check=lambda: None,
) -> str:
    """Upload a photo to a Facebook Page. Returns the Graph post/photo id."""
    if not image_path.is_file():
        raise MetaAPIError('Image file not found for Facebook publish.')
    url = f'{graph_base()}/{page_id}/photos'
    mime = _image_mime(image_path)
    cancel_check()
    with image_path.open('rb') as fh:
        files = {'source': (image_path.name, fh, mime)}
        data = {'caption': caption, 'access_token': page_token, 'published': 'true'}
        with httpx.Client(timeout=120.0) as client:
            resp = client.post(url, data=data, files=files)
            result = resp.json()
    _raise_for_graph(result, fallback='Facebook photo publish failed')
    post_id = result.get('post_id') or result.get('id')
    if not post_id:
        raise MetaAPIError('Facebook publish succeeded but no post id was returned.')
    return str(post_id)


def _image_mime(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in {'.jpg', '.jpeg'}:
        return 'image/jpeg'
    if ext == '.webp':
        return 'image/webp'
    if ext == '.gif':
        return 'image/gif'
    return 'image/png'


def publish_facebook_carousel(
    *,
    page_id: str,
    page_token: str,
    image_paths: list[Path],
    caption: str,
    cancel_check=lambda: None,
) -> str:
    """Upload multiple unpublished photos, then publish as one multi-photo feed post."""
    paths = [Path(p) for p in image_paths if p]
    if len(paths) < 2:
        raise MetaAPIError('Facebook multi-photo post needs at least 2 images.')

    media_fbids: list[str] = []
    url = f'{graph_base()}/{page_id}/photos'
    with httpx.Client(timeout=120.0) as client:
        for image_path in paths[:10]:
            cancel_check()
            if not image_path.is_file():
                raise MetaAPIError(f'Image file not found: {image_path.name}')
            mime = _image_mime(image_path)
            with image_path.open('rb') as fh:
                files = {'source': (image_path.name, fh, mime)}
                data = {
                    'published': 'false',
                    'temporary': 'true',
                    'access_token': page_token,
                }
                resp = client.post(url, data=data, files=files)
                result = resp.json()
            logger.info('Facebook unpublished photo upload status=%s body_keys=%s', resp.status_code, list(result) if isinstance(result, dict) else type(result))
            _raise_for_graph(result, fallback='Facebook multi-photo upload failed')
            mid = result.get('id')
            if not mid:
                raise MetaAPIError('Facebook did not return a photo id for multi-photo item.')
            media_fbids.append(str(mid))

    # Unpublished uploads are harmless; this is the point that makes the
    # carousel visible on Facebook.
    cancel_check()
    feed_url = f'{graph_base()}/{page_id}/feed'
    # Graph expects attached_media[n] as a JSON object string: {"media_fbid":"..."}
    data = {
        'message': caption,
        'access_token': page_token,
    }
    for i, mid in enumerate(media_fbids):
        data[f'attached_media[{i}]'] = f'{{"media_fbid":"{mid}"}}'
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(feed_url, data=data)
        published = resp.json()
    logger.info('Facebook multi-photo feed status=%s', resp.status_code)
    _raise_for_graph(published, fallback='Facebook multi-photo publish failed')
    post_id = published.get('id')
    if not post_id:
        raise MetaAPIError('Facebook multi-photo publish succeeded but no post id was returned.')
    return str(post_id)


def publish_facebook_video(
    *,
    page_id: str,
    page_token: str,
    video_path: Path,
    caption: str,
    cancel_check=lambda: None,
) -> str:
    """Upload a video to a Facebook Page. Returns the video id."""
    if not video_path.is_file():
        raise MetaAPIError('Video file not found for Facebook publish.')
    url = f'{graph_base()}/{page_id}/videos'
    # Facebook's single-video endpoint publishes as part of the upload. Once
    # this POST starts, Meta may complete it even if the local request closes.
    cancel_check()
    with video_path.open('rb') as fh:
        files = {'source': (video_path.name, fh, 'video/mp4')}
        data = {
            'description': caption,
            'access_token': page_token,
            'published': 'true',
        }
        with httpx.Client(timeout=300.0) as client:
            resp = client.post(url, data=data, files=files)
            result = resp.json()
    _raise_for_graph(result, fallback='Facebook video publish failed')
    video_id = result.get('id')
    if not video_id:
        raise MetaAPIError('Facebook video publish succeeded but no id was returned.')
    return str(video_id)


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


def publish_post_to_meta(
    post,
    *,
    platforms: set[str] | None = None,
    cancel_check=lambda: None,
) -> dict:
    """
    Publish a post to Facebook and/or Instagram via Graph API.
    Supports single image, carousel (multi-image), and video.
    platforms: optional subset {'facebook', 'instagram'}; defaults to post flags.
    Returns {'facebook': id|None, 'instagram': id|None}.
    """
    from django.utils import timezone

    from .models import Post

    want_fb = (
        post.publish_to_facebook if platforms is None else 'facebook' in platforms
    ) and not post.facebook_post_id
    want_ig = (
        post.publish_to_instagram if platforms is None else 'instagram' in platforms
    ) and not post.instagram_media_id

    if not want_fb and not want_ig:
        return {'facebook': None, 'instagram': None}

    cancel_check()
    is_video = post.media_type == Post.MEDIA_VIDEO or bool(post.video)
    carousel_paths = post.carousel_image_paths() if not is_video else []
    is_carousel = (not is_video) and (
        post.media_type == Post.MEDIA_CAROUSEL or len(carousel_paths) >= 2
    )

    if is_video:
        if not post.video:
            raise MetaAPIError('A video file is required to publish video content.')
    elif is_carousel:
        if len(carousel_paths) < 2:
            raise MetaAPIError('A carousel needs at least 2 images.')
    elif not post.image:
        raise MetaAPIError('An image is required to publish to Facebook or Instagram.')

    caption = post_caption_text(post)
    errors: list[str] = []
    result = {'facebook': None, 'instagram': None}
    update_fields: list[str] = []

    from .instagram_login import (
        InstagramStillProcessing,
        instagram_publish_image_url,
        is_instagram_not_ready,
        public_video_url_for_instagram,
        publish_instagram_login_carousel,
        publish_instagram_login_photo,
        publish_instagram_login_video,
        resolve_instagram_login_credentials,
    )

    # Resolve credentials, file paths, and public URLs up front so the network
    # calls below can run on worker threads without touching the DB or ORM.
    fb_task = None
    if want_fb:
        try:
            creds = resolve_publish_credentials(post.user)
            if is_video:
                fb_task = lambda: publish_facebook_video(  # noqa: E731
                    page_id=creds['page_id'],
                    page_token=creds['page_token'],
                    video_path=Path(post.video.path),
                    caption=caption,
                    cancel_check=cancel_check,
                )
            elif is_carousel:
                fb_task = lambda: publish_facebook_carousel(  # noqa: E731
                    page_id=creds['page_id'],
                    page_token=creds['page_token'],
                    image_paths=carousel_paths,
                    caption=caption,
                    cancel_check=cancel_check,
                )
            else:
                fb_task = lambda: publish_facebook_photo(  # noqa: E731
                    page_id=creds['page_id'],
                    page_token=creds['page_token'],
                    image_path=Path(post.image.path),
                    caption=caption,
                    cancel_check=cancel_check,
                )
        except MetaAPIError as exc:
            errors.append(f'Facebook: {exc}')

    ig_task = None
    ig_creds = None
    if want_ig:
        try:
            ig_creds = resolve_instagram_login_credentials(post.user)
            if is_video:
                video_url = public_video_url_for_instagram(post.video.url)
                ig_task = lambda: publish_instagram_login_video(  # noqa: E731
                    ig_user_id=ig_creds['ig_user_id'],
                    access_token=ig_creds['access_token'],
                    video_url=video_url,
                    caption=caption,
                    cancel_check=cancel_check,
                )
            elif is_carousel:
                urls = []
                for item in post.ordered_media():
                    path = item.resolve_image_path()
                    url = item.resolve_image_url()
                    if path and url:
                        urls.append(instagram_publish_image_url(path, url))
                if len(urls) < 2 and post.image:
                    # Fallback: cover + media items missing URLs
                    urls = [instagram_publish_image_url(Path(post.image.path), post.image.url)]
                if len(urls) < 2:
                    for p in carousel_paths:
                        # Build media URL from path under MEDIA_ROOT
                        rel = str(Path(p).relative_to(Path(settings.MEDIA_ROOT))).replace('\\', '/')
                        media_url = f'{settings.MEDIA_URL.rstrip("/")}/{rel}'
                        urls.append(instagram_publish_image_url(p, media_url))
                ig_task = lambda: publish_instagram_login_carousel(  # noqa: E731
                    ig_user_id=ig_creds['ig_user_id'],
                    access_token=ig_creds['access_token'],
                    image_urls=urls,
                    caption=caption,
                    cancel_check=cancel_check,
                )
            else:
                image_url = instagram_publish_image_url(Path(post.image.path), post.image.url)
                ig_task = lambda: publish_instagram_login_photo(  # noqa: E731
                    ig_user_id=ig_creds['ig_user_id'],
                    access_token=ig_creds['access_token'],
                    image_url=image_url,
                    caption=caption,
                    cancel_check=cancel_check,
                )
        except MetaAPIError as exc:
            if isinstance(exc, InstagramStillProcessing) or is_instagram_not_ready(exc):
                logger.warning('Instagram not ready for post id=%s', post.pk)
            else:
                errors.append(f'Instagram: {exc}')

    fb_id, fb_exc, ig_id, ig_exc = _run_publish_tasks(fb_task, ig_task)

    if fb_exc is not None:
        if not isinstance(fb_exc, MetaAPIError):
            logger.exception(
                'Unexpected Facebook publish error for post id=%s',
                post.pk,
                exc_info=fb_exc,
            )
        errors.append(f'Facebook: {fb_exc}')
    elif fb_id:
        result['facebook'] = fb_id
        post.facebook_post_id = fb_id
        post.facebook_published_at = timezone.now()
        post.publish_to_facebook = True
        update_fields.extend([
            'facebook_post_id',
            'facebook_published_at',
            'publish_to_facebook',
        ])
        logger.info('Published post id=%s to Facebook', post.pk)

    if ig_exc is not None:
        if isinstance(ig_exc, InstagramStillProcessing) or (
            isinstance(ig_exc, MetaAPIError) and is_instagram_not_ready(ig_exc)
        ):
            logger.warning(
                'Instagram still processing post id=%s; not showing 9007 to the user',
                post.pk,
            )
        else:
            if not isinstance(ig_exc, MetaAPIError):
                logger.exception(
                    'Unexpected Instagram publish error for post id=%s',
                    post.pk,
                    exc_info=ig_exc,
                )
            errors.append(f'Instagram: {ig_exc}')
    elif ig_id:
        result['instagram'] = ig_id
        post.instagram_media_id = ig_id
        post.instagram_published_at = timezone.now()
        post.publish_to_instagram = True
        update_fields.extend([
            'instagram_media_id',
            'instagram_published_at',
            'publish_to_instagram',
        ])
        logger.info('Published post id=%s to Instagram', post.pk)

    if update_fields:
        update_fields.append('updated_at')
        post.save(update_fields=list(dict.fromkeys(update_fields)))

    if errors:
        raise MetaAPIError(' · '.join(errors))
    return result


def _run_publish_tasks(fb_task, ig_task):
    """
    Run the Facebook and Instagram uploads at the same time so the user waits
    for the slower one instead of both. Returns (fb_id, fb_exc, ig_id, ig_exc).
    """
    def _call(task):
        if task is None:
            return None, None
        try:
            return task(), None
        except Exception as exc:
            return None, exc

    if fb_task is not None and ig_task is not None:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix='meta-publish') as pool:
            fb_future = pool.submit(_call, fb_task)
            ig_future = pool.submit(_call, ig_task)
            fb_id, fb_exc = fb_future.result()
            ig_id, ig_exc = ig_future.result()
        return fb_id, fb_exc, ig_id, ig_exc

    fb_id, fb_exc = _call(fb_task)
    ig_id, ig_exc = _call(ig_task)
    return fb_id, fb_exc, ig_id, ig_exc
