from django.urls import path

from . import views

app_name = 'posts'

urlpatterns = [
    path('posts/create/', views.post_create_view, name='create'),
    path('posts/generate-image/', views.generate_image_view, name='generate_image'),
    path('posts/media/', views.media_library_view, name='media_library'),
    path('posts/media/picker/', views.media_picker_api_view, name='media_picker'),
    path('posts/media/upload/', views.media_upload_view, name='media_upload'),
    path('posts/media/<int:pk>/delete/', views.media_delete_view, name='media_delete'),
    path('posts/<int:pk>/edit/', views.post_edit_view, name='edit'),
    path('posts/<int:pk>/duplicate/', views.post_duplicate_view, name='duplicate'),
    path('posts/<int:pk>/delete/', views.post_delete_view, name='delete'),
    path('posts/<int:pk>/preview/', views.post_preview_view, name='preview'),
    path('posts/<int:pk>/cancel-publish/', views.cancel_publish_view, name='cancel_publish'),
    path('posts/<int:pk>/publish-platform/', views.publish_platform_view, name='publish_platform'),
]
