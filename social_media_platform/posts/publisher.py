"""Publish posts whose scheduled_at time has arrived."""

from __future__ import annotations

import logging

from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)


def publish_post(post) -> None:
    """
    Publish one post to selected social platforms (if any), then mark published.
    Raises on social API failure (caller should mark failed).
    """
    from .meta import publish_post_to_meta

    publish_post_to_meta(post)
    post.mark_published()


def publish_due_posts() -> int:
    """Publish all due scheduled posts to Meta (when selected) and mark published."""
    from .models import Post

    now = timezone.now()
    due_qs = Post.objects.filter(
        status=Post.STATUS_SCHEDULED,
    ).filter(
        Q(scheduled_at__lte=now) | Q(scheduled_at__isnull=True),
    ).select_related('user', 'user__profile')

    published = 0
    for post in due_qs.iterator():
        try:
            publish_post(post)
            published += 1
        except Exception:
            logger.exception('Failed to publish scheduled post id=%s', post.pk)
            try:
                Post.objects.filter(pk=post.pk).update(status=Post.STATUS_FAILED)
            except Exception:
                logger.exception('Failed to mark post id=%s as failed', post.pk)

    if published:
        logger.info('Published %s scheduled post(s)', published)
    return published
