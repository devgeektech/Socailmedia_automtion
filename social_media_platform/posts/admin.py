from django.contrib import admin

from .models import Post


@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'user',
        'status',
        'scheduled_at',
        'published_at',
        'created_at',
    )
    list_filter = ('status', 'publish_to_facebook', 'publish_to_instagram')
    search_fields = ('description', 'caption', 'image_prompt', 'user__username')
    readonly_fields = ('created_at', 'updated_at')
