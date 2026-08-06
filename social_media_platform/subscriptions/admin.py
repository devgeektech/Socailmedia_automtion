from django.contrib import admin

from .models import Plan, UserSubscription


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ('name', 'plan_type', 'price', 'duration_days', 'is_active', 'is_popular')
    prepopulated_fields = {'slug': ('name',)}


@admin.register(UserSubscription)
class UserSubscriptionAdmin(admin.ModelAdmin):
    list_display = ('user', 'plan', 'status', 'start_date', 'expiry_date', 'is_trial')
    list_filter = ('status', 'is_trial', 'plan')
    search_fields = ('user__username', 'user__email')
