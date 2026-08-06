from django.urls import path

from . import views

app_name = 'subscriptions'

urlpatterns = [
    path('plans/', views.plans_view, name='plans'),
    path('plans/select/<slug:slug>/', views.select_plan_view, name='select_plan'),
    path('dashboard/', views.dashboard_view, name='dashboard'),
]
