"""Background publishing for immediate and scheduled posts."""

from __future__ import annotations

import logging
import threading
from datetime import timedelta

from django.db import close_old_connections
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

_active_post_ids: set[int] = set()
_active_lock = threading.Lock()


def publish_post(post) -> None:
    """
    Publish one post to selected social platforms (if any), then mark published.
    Raises on social API failure (caller should mark failed).
    """
    from .meta import publish_post_to_meta

    publish_post_to_meta(post)
    post.mark_published()


def _publish_one(post_id: int, media_job: dict | None = None) -> None:
    """Worker entry point for one queued post."""
    from .media_utils import attach_media_job
    from .models import Post

    close_old_connections()
    try:
        post = Post.objects.select_related('user', 'user__profile').filter(
            pk=post_id,
            status=Post.STATUS_PUBLISHING,
        ).first()
        if post is None:
            return

        # Finish media attach off the request thread so Create Post can redirect fast.
        if media_job is not None:
            attach_media_job(post, media_job)
            post.refresh_from_db()

        if not post.image and not post.video and not post.media_items.exists():
            raise RuntimeError('Post has no media to publish.')

        publish_post(post)
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


def enqueue_publish(post_id: int, media_job: dict | None = None) -> bool:
    """Start one daemon worker per post and return without blocking the request."""
    with _active_lock:
        if post_id in _active_post_ids:
            return False
        _active_post_ids.add(post_id)
    thread = threading.Thread(
        target=_publish_one,
        kwargs={'post_id': post_id, 'media_job': media_job},
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
            publish_started_at=now,
        )
        if updated and enqueue_publish(post_id):
            queued += 1

    # A process restart can leave jobs marked publishing. Only recover stale
    # jobs; another web process may currently own a recent job.
    stale_before = now - timedelta(minutes=30)
    publishing_ids = list(Post.objects.filter(
        status=Post.STATUS_PUBLISHING,
    ).filter(
        Q(publish_started_at__lte=stale_before)
        | Q(publish_started_at__isnull=True)
    ).values_list('pk', flat=True))
    for post_id in publishing_ids:
        if enqueue_publish(post_id):
            queued += 1

    if queued:
        logger.info('Queued %s post(s) for background publishing', queued)
    return queued
