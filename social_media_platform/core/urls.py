from django.urls import path

from . import views

app_name = 'core'

urlpatterns = [
    path('', views.home_view, name='home'),
    path('privacy-policy/', views.privacy_policy_view, name='privacy_policy'),
    path('terms-of-service/', views.terms_of_service_view, name='terms_of_service')
]
