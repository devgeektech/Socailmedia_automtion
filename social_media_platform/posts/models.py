from django.conf import settings
from django.db import models
from django.utils import timezone


class Post(models.Model):
    STATUS_DRAFT = 'draft'
    STATUS_SCHEDULED = 'scheduled'
    STATUS_PUBLISHED = 'published'
    STATUS_FAILED = 'failed'

    STATUS_CHOICES = [
        (STATUS_DRAFT, 'Draft'),
        (STATUS_SCHEDULED, 'Scheduled'),
        (STATUS_PUBLISHED, 'Published'),
        (STATUS_FAILED, 'Failed'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='posts',
    )
    description = models.TextField(help_text='Main post description / body')
    caption = models.TextField(blank=True, help_text='Short caption for social platforms')
    image_prompt = models.TextField(
        blank=True,
        help_text='Prompt used to generate the post image with OpenAI',
    )
    image = models.ImageField(upload_to='posts/images/', blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    scheduled_at = models.DateTimeField(blank=True, null=True)
    published_at = models.DateTimeField(blank=True, null=True)

    publish_to_facebook = models.BooleanField(default=False)
    publish_to_instagram = models.BooleanField(default=False)
    facebook_post_id = models.CharField(max_length=64, blank=True)
    instagram_media_id = models.CharField(max_length=64, blank=True)
    facebook_published_at = models.DateTimeField(blank=True, null=True)
    instagram_published_at = models.DateTimeField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.user.username}: {self.description[:40]}'

    @property
    def is_scheduled(self):
        return self.status == self.STATUS_SCHEDULED

    @property
    def is_published(self):
        return self.status == self.STATUS_PUBLISHED

    @property
    def can_edit(self):
        return self.status in {self.STATUS_DRAFT, self.STATUS_SCHEDULED, self.STATUS_FAILED}

    def mark_published(self):
        self.status = self.STATUS_PUBLISHED
        self.published_at = timezone.now()
        # Keep scheduled_at for history when this was a scheduled post
        self.save(update_fields=['status', 'published_at', 'updated_at'])

    def mark_scheduled(self, when):
        self.status = self.STATUS_SCHEDULED
        self.scheduled_at = when
        self.published_at = None
        self.save(update_fields=['status', 'scheduled_at', 'published_at', 'updated_at'])

    def mark_draft(self):
        self.status = self.STATUS_DRAFT
        self.published_at = None
        self.save(update_fields=['status', 'published_at', 'updated_at'])
