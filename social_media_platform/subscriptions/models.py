from datetime import timedelta

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone


class Plan(models.Model):
    PLAN_TYPES = [
        ('free_trial', 'Free Trial'),
        ('monthly', 'Monthly'),
    ]

    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)
    plan_type = models.CharField(max_length=20, choices=PLAN_TYPES)
    price = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    duration_days = models.PositiveIntegerField(default=30)
    features = models.JSONField(default=list)
    is_active = models.BooleanField(default=True)
    is_popular = models.BooleanField(default=False)
    stripe_product_id = models.CharField(max_length=120, blank=True)
    stripe_price_id = models.CharField(max_length=120, blank=True)

    def __str__(self):
        return self.name


class UserSubscription(models.Model):
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('inactive', 'Inactive'),
        ('expired', 'Expired'),
        ('canceled', 'Canceled'),
        ('past_due', 'Past Due'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='subscriptions')
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    start_date = models.DateTimeField(auto_now_add=True)
    expiry_date = models.DateTimeField()
    is_trial = models.BooleanField(default=False)

    # Stripe billing fields (populated when Stripe is connected)
    stripe_customer_id = models.CharField(max_length=120, blank=True)
    stripe_subscription_id = models.CharField(max_length=120, blank=True)
    stripe_payment_intent_id = models.CharField(max_length=120, blank=True)
    latest_invoice_id = models.CharField(max_length=120, blank=True)
    latest_receipt_url = models.URLField(max_length=500, blank=True)
    latest_payment_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    latest_payment_currency = models.CharField(max_length=10, blank=True, default='usd')
    latest_payment_status = models.CharField(max_length=40, blank=True)
    latest_payment_date = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-start_date']

    def __str__(self):
        return f'{self.user.username} — {self.plan.name} ({self.status})'

    @property
    def is_valid(self):
        return self.status == 'active' and self.expiry_date > timezone.now()

    def activate(self):
        self.status = 'active'
        self.expiry_date = timezone.now() + timedelta(days=self.plan.duration_days)
        self.save()
