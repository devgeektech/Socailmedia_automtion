"""Lightweight background loop to publish due scheduled posts."""

from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

_started = False
_lock = threading.Lock()
POLL_SECONDS = 30


def start_publish_scheduler():
    """Start a daemon thread that publishes due posts every POLL_SECONDS."""
    global _started
    import sys

    # Under Django runserver autoreload, only start in the child process.
    using_runserver = any('runserver' in arg for arg in sys.argv)
    if using_runserver and os.environ.get('RUN_MAIN') != 'true':
        return

    with _lock:
        if _started:
            return
        _started = True

    def _loop():
        # Delay first run slightly so Django finishes booting.
        time.sleep(5)
        while True:
            try:
                from .publisher import publish_due_posts

                publish_due_posts()
            except Exception:
                logger.exception('Scheduled post publisher loop error')
            time.sleep(POLL_SECONDS)

    thread = threading.Thread(target=_loop, name='publish-due-posts', daemon=True)
    thread.start()
    logger.info('Started scheduled-post publisher (every %ss)', POLL_SECONDS)
