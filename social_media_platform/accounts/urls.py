from django.contrib.auth.views import (
    LogoutView,
    PasswordResetCompleteView,
    PasswordResetDoneView,
)
from django.urls import path, reverse_lazy

from . import views

app_name = 'accounts'

urlpatterns = [
    path('login/', views.CustomLoginView.as_view(), name='login'),
    path('signup/', views.signup_view, name='signup'),
    path(
        'logout/',
        LogoutView.as_view(next_page=reverse_lazy('core:home')),
        name='logout',
    ),
    path('forgot-password/', views.CustomPasswordResetView.as_view(), name='forgot_password'),
    path(
        'forgot-password/done/',
        PasswordResetDoneView.as_view(template_name='accounts/forgot_password_done.html'),
        name='password_reset_done',
    ),
    path(
        'reset/<uidb64>/<token>/',
        views.CustomPasswordResetConfirmView.as_view(),
        name='password_reset_confirm',
    ),
    path(
        'reset/done/',
        PasswordResetCompleteView.as_view(template_name='accounts/reset_password_done.html'),
        name='password_reset_complete',
    ),
    path('social/', views.social_connections_view, name='social_connections'),
    path('meta/connect/', views.meta_connect_view, name='meta_connect'),
    path('meta/callback/', views.meta_callback_view, name='meta_callback'),
    path('meta/select-page/', views.meta_select_page_view, name='meta_select_page'),
    path('meta/disconnect/', views.meta_disconnect_view, name='meta_disconnect'),
]
