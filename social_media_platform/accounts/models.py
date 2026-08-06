from django.db import models
from django.contrib.auth.models import User


class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    phone = models.CharField(max_length=20, blank=True)
    avatar = models.ImageField(upload_to='avatars/', blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # Meta / Facebook Page + Instagram Business connection
    facebook_user_id = models.CharField(max_length=64, blank=True)
    facebook_user_access_token = models.TextField(blank=True)
    facebook_page_id = models.CharField(max_length=64, blank=True)
    facebook_page_name = models.CharField(max_length=255, blank=True)
    facebook_page_access_token = models.TextField(blank=True)
    instagram_business_account_id = models.CharField(max_length=64, blank=True)
    instagram_username = models.CharField(max_length=255, blank=True)
    meta_connected_at = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return self.user.email or self.user.username

    @property
    def meta_connected(self):
        return bool(self.facebook_page_id and self.facebook_page_access_token)

    @property
    def facebook_ready(self):
        return self.meta_connected

    @property
    def instagram_ready(self):
        return self.meta_connected and bool(self.instagram_business_account_id)

    def apply_page_connection(self, *, page: dict, fb_user_id: str = '', user_token: str = ''):
        """Save a selected Page (and optional linked Instagram) onto this profile."""
        ig_id = str(page.get('instagram_id') or '')
        ig_username = str(page.get('instagram_username') or '')
        # Support raw Graph page shape as well as serialized session shape
        if not ig_id:
            ig = page.get('instagram_business_account') or {}
            ig_id = str(ig.get('id') or '')
            ig_username = str(ig.get('username') or '') or ig_username

        if fb_user_id:
            self.facebook_user_id = fb_user_id
        if user_token:
            self.facebook_user_access_token = user_token

        self.facebook_page_id = str(page.get('id') or '')
        self.facebook_page_name = str(page.get('name') or '')
        self.facebook_page_access_token = str(page.get('access_token') or '')
        self.instagram_business_account_id = ig_id
        self.instagram_username = ig_username
        from django.utils import timezone
        self.meta_connected_at = timezone.now()
        self.save()

    def clear_instagram_connection(self):
        self.instagram_business_account_id = ''
        self.instagram_username = ''
        self.save(update_fields=['instagram_business_account_id', 'instagram_username'])

    def clear_meta_connection(self):
        self.facebook_user_id = ''
        self.facebook_user_access_token = ''
        self.facebook_page_id = ''
        self.facebook_page_name = ''
        self.facebook_page_access_token = ''
        self.instagram_business_account_id = ''
        self.instagram_username = ''
        self.meta_connected_at = None
        self.save(update_fields=[
            'facebook_user_id',
            'facebook_user_access_token',
            'facebook_page_id',
            'facebook_page_name',
            'facebook_page_access_token',
            'instagram_business_account_id',
            'instagram_username',
            'meta_connected_at',
        ])
