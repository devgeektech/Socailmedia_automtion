"""Cancellable background publishing for immediate and scheduled posts."""

from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta

from django.db import close_old_connections
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

_active_post_ids: set[int] = set()
_active_lock = threading.Lock()


class PublishCancelled(Exception):
    """Publishing was cancelled before Meta accepted the remaining work."""


def ensure_not_cancelled(post_id: int) -> None:
    """Read the durable cancel flag so cancellation works across threads."""
    from .models import Post

    if Post.objects.filter(pk=post_id, cancel_requested=True).exists():
        raise PublishCancelled('Publishing cancelled by the user.')


def publish_post(post) -> None:
    """
    Publish one post to selected social platforms (if any), then mark published.
    Raises on social API failure (caller should mark failed).
    """
    from .meta import publish_post_to_meta

    ensure_not_cancelled(post.pk)
    publish_post_to_meta(post, cancel_check=lambda: ensure_not_cancelled(post.pk))
    post.refresh_from_db()
    if post.cancel_requested:
        if post.facebook_post_id or post.instagram_media_id:
            post.mark_published()
        else:
            post.mark_draft()
        return
    post.mark_published()


def _publish_one(post_id: int, *, grace_seconds: float = 0.0) -> None:
    """Worker entry point. The grace window lets the user cancel immediately."""
    from .models import Post

    close_old_connections()
    try:
        if grace_seconds:
            deadline = time.monotonic() + grace_seconds
            while time.monotonic() < deadline:
                ensure_not_cancelled(post_id)
                time.sleep(min(0.25, deadline - time.monotonic()))

        post = Post.objects.select_related('user', 'user__profile').filter(
            pk=post_id,
            status=Post.STATUS_PUBLISHING,
        ).first()
        if post is None:
            return
        publish_post(post)
    except PublishCancelled:
        post = Post.objects.filter(pk=post_id).first()
        if post and post.status == Post.STATUS_PUBLISHING:
            if post.facebook_post_id or post.instagram_media_id:
                post.mark_published()
            else:
                post.mark_draft()
        logger.info('Publishing cancelled for post id=%s', post_id)
    except Exception:
        logger.exception('Failed to publish post id=%s', post_id)
        Post.objects.filter(
            pk=post_id,
            status=Post.STATUS_PUBLISHING,
        ).update(status=Post.STATUS_FAILED)
    finally:
        close_old_connections()
        with _active_lock:
            _active_post_ids.discard(post_id)


def enqueue_publish(post_id: int, *, grace_seconds: float = 3.0) -> bool:
    """Start one daemon worker per post and return without blocking the request."""
    with _active_lock:
        if post_id in _active_post_ids:
            return False
        _active_post_ids.add(post_id)
    thread = threading.Thread(
        target=_publish_one,
        kwargs={'post_id': post_id, 'grace_seconds': grace_seconds},
        name=f'publish-post-{post_id}',
        daemon=True,
    )
    thread.start()
    return True


def publish_due_posts() -> int:
    """Queue due scheduled posts and recover interrupted publishing jobs."""
    from .models import Post

    now = timezone.now()
    due_ids = list(Post.objects.filter(
        status=Post.STATUS_SCHEDULED,
    ).filter(
        Q(scheduled_at__lte=now) | Q(scheduled_at__isnull=True),
    ).values_list('pk', flat=True))

    queued = 0
    for post_id in due_ids:
        updated = Post.objects.filter(
            pk=post_id,
            status=Post.STATUS_SCHEDULED,
        ).update(
            status=Post.STATUS_PUBLISHING,
            cancel_requested=False,
            publish_started_at=now,
        )
        if updated and enqueue_publish(post_id, grace_seconds=0):
            queued += 1

    # A process restart can leave jobs marked publishing. Only recover stale
    # jobs; another web process may currently own a recent job.
    stale_before = now - timedelta(minutes=30)
    publishing_ids = list(Post.objects.filter(
        status=Post.STATUS_PUBLISHING,
        cancel_requested=False,
    ).filter(
        Q(publish_started_at__lte=stale_before)
        | Q(publish_started_at__isnull=True)
    ).values_list('pk', flat=True))
    for post_id in publishing_ids:
        if enqueue_publish(post_id, grace_seconds=0):
            queued += 1

    cancelled_ids = list(Post.objects.filter(
        status=Post.STATUS_PUBLISHING,
        cancel_requested=True,
    ).values_list('pk', flat=True))
    for post_id in cancelled_ids:
        post = Post.objects.filter(pk=post_id).first()
        if post and not (post.facebook_post_id or post.instagram_media_id):
            post.mark_draft()

    if queued:
        logger.info('Queued %s post(s) for background publishing', queued)
    return queued
