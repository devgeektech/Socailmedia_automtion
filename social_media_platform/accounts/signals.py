from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from .models import UserProfile


@receiver(pre_save, sender=User)
def ensure_unique_user_email(sender, instance, **kwargs):
    """Normalize email and block duplicate addresses (case-insensitive)."""
    if instance.email:
        instance.email = instance.email.strip().lower()
        qs = User.objects.filter(email__iexact=instance.email)
        if instance.pk:
            qs = qs.exclude(pk=instance.pk)
        if qs.exists():
            raise ValidationError({'email': 'An account with this email already exists.'})


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.create(user=instance)
