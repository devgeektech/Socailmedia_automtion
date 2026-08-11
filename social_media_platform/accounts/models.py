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
    # Instagram Login (separate from Facebook Page)
    instagram_user_id = models.CharField(max_length=64, blank=True)
    instagram_access_token = models.TextField(blank=True)
    instagram_token_expires_at = models.DateTimeField(blank=True, null=True)
    instagram_connected_at = models.DateTimeField(blank=True, null=True)
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
        token = (self.instagram_access_token or '').strip()
        user_id = (self.instagram_user_id or self.instagram_business_account_id or '').strip()
        return bool(token and user_id)

    def apply_page_connection(self, *, page: dict, fb_user_id: str = '', user_token: str = ''):
        """Save a selected Facebook Page. Does not change Instagram Login fields."""
        if fb_user_id:
            self.facebook_user_id = fb_user_id
        if user_token:
            self.facebook_user_access_token = user_token

        self.facebook_page_id = str(page.get('id') or '')
        self.facebook_page_name = str(page.get('name') or '')
        self.facebook_page_access_token = str(page.get('access_token') or '')
        from django.utils import timezone
        self.meta_connected_at = timezone.now()
        self.save(update_fields=[
            'facebook_user_id',
            'facebook_user_access_token',
            'facebook_page_id',
            'facebook_page_name',
            'facebook_page_access_token',
            'meta_connected_at',
        ])

    def apply_instagram_login(
        self,
        *,
        user_id: str,
        access_token: str,
        username: str = '',
        expires_at=None,
    ):
        """Save Instagram Login tokens. Does not change Facebook Page fields."""
        from django.utils import timezone

        self.instagram_user_id = str(user_id or '')
        self.instagram_access_token = access_token or ''
        self.instagram_username = username or ''
        self.instagram_token_expires_at = expires_at
        self.instagram_connected_at = timezone.now()
        self.instagram_business_account_id = str(user_id or '')
        self.save(update_fields=[
            'instagram_user_id',
            'instagram_access_token',
            'instagram_username',
            'instagram_token_expires_at',
            'instagram_connected_at',
            'instagram_business_account_id',
        ])

    def clear_instagram_connection(self):
        self.instagram_business_account_id = ''
        self.instagram_username = ''
        self.instagram_user_id = ''
        self.instagram_access_token = ''
        self.instagram_token_expires_at = None
        self.instagram_connected_at = None
        self.save(update_fields=[
            'instagram_business_account_id',
            'instagram_username',
            'instagram_user_id',
            'instagram_access_token',
            'instagram_token_expires_at',
            'instagram_connected_at',
        ])

    def clear_meta_connection(self):
        """Disconnect Facebook Page only — Instagram Login stays."""
        self.facebook_user_id = ''
        self.facebook_user_access_token = ''
        self.facebook_page_id = ''
        self.facebook_page_name = ''
        self.facebook_page_access_token = ''
        self.meta_connected_at = None
        self.save(update_fields=[
            'facebook_user_id',
            'facebook_user_access_token',
            'facebook_page_id',
            'facebook_page_name',
            'facebook_page_access_token',
            'meta_connected_at',
        ])
