"""Instagram API with Instagram Login (no Facebook Page required)."""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from urllib.parse import urlencode

import httpx
from django.conf import settings
from django.utils import timezone

from .meta import MetaAPIError, _raise_for_graph, absolute_media_url

logger = logging.getLogger(__name__)

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
        'force_reauth': 'true',
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


def _wait_for_ig_login_container(creation_id: str, access_token: str, *, attempts: int = 12) -> None:
    url = f'{ig_graph_base()}/{creation_id}'
    with httpx.Client(timeout=30.0) as client:
        for _ in range(attempts):
            resp = client.get(
                url,
                params={'fields': 'status_code,status', 'access_token': access_token},
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

    publish_url = f'{ig_graph_base()}/{ig_user_id}/media_publish'
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(
            publish_url,
            data={'creation_id': creation_id, 'access_token': access_token},
        )
        published = resp.json()
    _raise_for_graph(published, fallback='Instagram media publish failed')
    media_id = published.get('id')
    if not media_id:
        raise MetaAPIError('Instagram publish succeeded but no media id was returned.')
    return str(media_id)


def public_image_url_for_instagram(image_url: str) -> str:
    url = absolute_media_url(image_url)
    if url.startswith('http://localhost') or url.startswith('http://127.0.0.1'):
        raise MetaAPIError(
            'Instagram needs a public HTTPS image URL. '
            'Set PUBLIC_BASE_URL to a public tunnel/domain Meta can reach.'
        )
    return url
