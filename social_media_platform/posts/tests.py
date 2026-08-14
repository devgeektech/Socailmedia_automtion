from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TransactionTestCase

from .forms import PostForm
from .models import Post
from .publisher import publish_post


class BackgroundPublishTests(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='publisher',
            email='publisher@example.com',
            password='test-password',
        )
        self.post = Post.objects.create(
            user=self.user,
            description='Test post',
            status=Post.STATUS_PUBLISHING,
            publish_to_facebook=False,
            publish_to_instagram=False,
        )

    @patch('posts.meta.publish_post_to_meta')
    def test_completed_worker_marks_post_published(self, publish_to_meta):
        publish_post(self.post)

        publish_to_meta.assert_called_once()
        self.post.refresh_from_db()
        self.assertEqual(self.post.status, Post.STATUS_PUBLISHED)
        self.assertIsNotNone(self.post.published_at)

    def test_publish_now_marks_publishing_without_blocking(self):
        form = PostForm(user=self.user)
        form.cleaned_data = {'publish_action': PostForm.PUBLISH_NOW}

        form.apply_publish_action(self.post)

        self.post.refresh_from_db()
        self.assertEqual(self.post.status, Post.STATUS_PUBLISHING)
        self.assertIsNotNone(self.post.publish_started_at)
