from django.contrib import admin

from .models import MediaAsset, Post, PostMedia


class PostMediaInline(admin.TabularInline):
    model = PostMedia
    extra = 0


@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'user',
        'media_type',
        'status',
        'scheduled_at',
        'published_at',
        'created_at',
    )
    list_filter = ('status', 'media_type', 'publish_to_facebook', 'publish_to_instagram')
    search_fields = ('description', 'caption', 'image_prompt', 'user__username')
    readonly_fields = ('created_at', 'updated_at')
    inlines = [PostMediaInline]


@admin.register(MediaAsset)
class MediaAssetAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'kind', 'source', 'original_name', 'created_at')
    list_filter = ('kind', 'source')
    search_fields = ('original_name', 'prompt', 'user__username')
