from django.urls import path

from . import views

app_name = 'dashboard'

urlpatterns = [
    path('', views.admin_root_view, name='root'),
    path('login/', views.admin_login_view, name='login'),
    path('logout/', views.admin_logout_view, name='logout'),
    path('overview/', views.admin_home_view, name='home'),
    path('users/', views.users_list_view, name='users'),
    path('users/<int:user_id>/', views.user_detail_view, name='user_detail'),
    path('users/<int:user_id>/toggle-active/', views.user_toggle_active_view, name='user_toggle_active'),
    path('users/<int:user_id>/assign-subscription/', views.assign_subscription_view, name='assign_subscription'),
    path('subscriptions/', views.subscriptions_list_view, name='subscriptions'),
    path('subscriptions/<int:sub_id>/', views.subscription_detail_view, name='subscription_detail'),
    path('posts/', views.posts_list_view, name='posts'),
    path('posts/<int:post_id>/', views.post_detail_view, name='post_detail'),
]
