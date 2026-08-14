from django.conf import settings
from django.db import models
from django.utils import timezone


class MediaAsset(models.Model):
    """Reusable uploaded or AI-generated media for the user's library."""

    KIND_IMAGE = 'image'
    KIND_VIDEO = 'video'
    KIND_CHOICES = [
        (KIND_IMAGE, 'Image'),
        (KIND_VIDEO, 'Video'),
    ]

    SOURCE_UPLOAD = 'upload'
    SOURCE_AI = 'ai'
    SOURCE_CHOICES = [
        (SOURCE_UPLOAD, 'Upload'),
        (SOURCE_AI, 'AI generated'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='media_assets',
    )
    file = models.FileField(upload_to='library/%Y/%m/')
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default=KIND_IMAGE)
    source = models.CharField(max_length=16, choices=SOURCE_CHOICES, default=SOURCE_UPLOAD)
    prompt = models.TextField(blank=True)
    original_name = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.user_id}:{self.kind}:{self.pk}'

    @property
    def is_image(self):
        return self.kind == self.KIND_IMAGE

    @property
    def is_video(self):
        return self.kind == self.KIND_VIDEO


class Post(models.Model):
    STATUS_DRAFT = 'draft'
    STATUS_PUBLISHING = 'publishing'
    STATUS_SCHEDULED = 'scheduled'
    STATUS_PUBLISHED = 'published'
    STATUS_FAILED = 'failed'

    STATUS_CHOICES = [
        (STATUS_DRAFT, 'Draft'),
        (STATUS_PUBLISHING, 'Publishing'),
        (STATUS_SCHEDULED, 'Scheduled'),
        (STATUS_PUBLISHED, 'Published'),
        (STATUS_FAILED, 'Failed'),
    ]

    MEDIA_IMAGE = 'image'
    MEDIA_CAROUSEL = 'carousel'
    MEDIA_VIDEO = 'video'
    MEDIA_TYPE_CHOICES = [
        (MEDIA_IMAGE, 'Single image'),
        (MEDIA_CAROUSEL, 'Carousel'),
        (MEDIA_VIDEO, 'Video'),
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
    media_type = models.CharField(
        max_length=16,
        choices=MEDIA_TYPE_CHOICES,
        default=MEDIA_IMAGE,
    )
    image = models.ImageField(upload_to='posts/images/', blank=True, null=True)
    video = models.FileField(upload_to='posts/videos/', blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    scheduled_at = models.DateTimeField(blank=True, null=True)
    published_at = models.DateTimeField(blank=True, null=True)
    cancel_requested = models.BooleanField(default=False)
    publish_started_at = models.DateTimeField(blank=True, null=True)

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
    def is_publishing(self):
        return self.status == self.STATUS_PUBLISHING

    @property
    def can_cancel_publish(self):
        return self.is_publishing and not self.cancel_requested

    @property
    def can_edit(self):
        return self.status in {self.STATUS_DRAFT, self.STATUS_SCHEDULED, self.STATUS_FAILED}

    @property
    def is_carousel(self):
        return self.media_type == self.MEDIA_CAROUSEL

    @property
    def is_video(self):
        return self.media_type == self.MEDIA_VIDEO

    def ordered_media(self):
        return self.media_items.select_related('asset').order_by('order', 'id')

    def carousel_image_paths(self):
        """Absolute filesystem paths for multi-photo slides (cover first if needed)."""
        from pathlib import Path

        paths = []
        for item in self.ordered_media():
            path = item.resolve_image_path()
            if path:
                paths.append(Path(path))
        if not paths and self.image:
            paths.append(Path(self.image.path))
        return paths

    def sync_media_type(self):
        if self.video:
            self.media_type = self.MEDIA_VIDEO
        elif self.media_items.count() >= 2:
            self.media_type = self.MEDIA_CAROUSEL
        else:
            self.media_type = self.MEDIA_IMAGE

    def mark_published(self):
        self.status = self.STATUS_PUBLISHED
        self.published_at = timezone.now()
        self.cancel_requested = False
        self.save(update_fields=['status', 'published_at', 'cancel_requested', 'updated_at'])

    def mark_publishing(self):
        self.status = self.STATUS_PUBLISHING
        self.scheduled_at = None
        self.published_at = None
        self.cancel_requested = False
        self.publish_started_at = timezone.now()
        self.save(update_fields=[
            'status',
            'scheduled_at',
            'published_at',
            'cancel_requested',
            'publish_started_at',
            'updated_at',
        ])

    def mark_scheduled(self, when):
        self.status = self.STATUS_SCHEDULED
        self.scheduled_at = when
        self.published_at = None
        self.cancel_requested = False
        self.publish_started_at = None
        self.save(update_fields=[
            'status',
            'scheduled_at',
            'published_at',
            'cancel_requested',
            'publish_started_at',
            'updated_at',
        ])

    def mark_draft(self):
        self.status = self.STATUS_DRAFT
        self.published_at = None
        self.cancel_requested = False
        self.publish_started_at = None
        self.save(update_fields=[
            'status',
            'published_at',
            'cancel_requested',
            'publish_started_at',
            'updated_at',
        ])

    def duplicate_for(self, user):
        """Create a draft copy for the user (new caption/date editable)."""
        from django.core.files.base import ContentFile

        clone = Post(
            user=user,
            description=self.description,
            caption=self.caption,
            image_prompt=self.image_prompt,
            media_type=self.media_type,
            status=self.STATUS_DRAFT,
            publish_to_facebook=self.publish_to_facebook,
            publish_to_instagram=self.publish_to_instagram,
        )
        clone.save()

        if self.image:
            self.image.open('rb')
            data = self.image.read()
            self.image.close()
            clone.image.save(
                f'copy_{self.pk}_{self.image.name.split("/")[-1]}',
                ContentFile(data),
                save=True,
            )

        if self.video:
            self.video.open('rb')
            data = self.video.read()
            self.video.close()
            clone.video.save(
                f'copy_{self.pk}_{self.video.name.split("/")[-1]}',
                ContentFile(data),
                save=True,
            )

        for item in self.ordered_media():
            pm = PostMedia(post=clone, asset=item.asset, order=item.order)
            if item.image:
                item.image.open('rb')
                data = item.image.read()
                item.image.close()
                pm.image.save(
                    f'copy_{item.pk}_{item.image.name.split("/")[-1]}',
                    ContentFile(data),
                    save=False,
                )
            pm.save()

        return clone


class PostMedia(models.Model):
    """Ordered image slide for a carousel (or multi-image) post."""

    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name='media_items')
    asset = models.ForeignKey(
        MediaAsset,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='post_links',
    )
    image = models.ImageField(upload_to='posts/carousel/', blank=True, null=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order', 'id']

    def resolve_image_path(self):
        from pathlib import Path

        if self.image:
            return Path(self.image.path)
        if self.asset_id and self.asset and self.asset.file:
            return Path(self.asset.file.path)
        return None

    def resolve_image_url(self):
        if self.image:
            return self.image.url
        if self.asset_id and self.asset and self.asset.file:
            return self.asset.file.url
        return ''
