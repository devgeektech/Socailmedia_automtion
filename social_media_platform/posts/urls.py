from django.urls import path

from . import views

app_name = 'posts'

urlpatterns = [
    path('posts/create/', views.post_create_view, name='create'),
    path('posts/generate-image/', views.generate_image_view, name='generate_image'),
    path('posts/<int:pk>/edit/', views.post_edit_view, name='edit'),
    path('posts/<int:pk>/delete/', views.post_delete_view, name='delete'),
    path('posts/<int:pk>/preview/', views.post_preview_view, name='preview'),
    path('posts/<int:pk>/publish-platform/', views.publish_platform_view, name='publish_platform'),
]
