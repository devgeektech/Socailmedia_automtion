from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User

from .models import UserProfile


class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    readonly_fields = (
        'facebook_user_id',
        'facebook_page_id',
        'facebook_page_name',
        'instagram_business_account_id',
        'instagram_username',
        'instagram_user_id',
        'instagram_connected_at',
        'meta_connected_at',
    )
    exclude = (
        'facebook_page_access_token',
        'facebook_user_access_token',
        'instagram_access_token',
    )


class UserAdmin(BaseUserAdmin):
    inlines = [UserProfileInline]


admin.site.unregister(User)
admin.site.register(User, UserAdmin)
admin.site.register(UserProfile)
