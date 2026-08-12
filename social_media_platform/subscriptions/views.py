from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .models import Plan, UserSubscription


@login_required
def plans_view(request):
    plans = Plan.objects.filter(is_active=True)
    current = UserSubscription.objects.filter(user=request.user, status='active').first()
    return render(request, 'subscriptions/plans.html', {
        'plans': plans,
        'current_subscription': current,
    })


@login_required
def select_plan_view(request, slug):
    plan = get_object_or_404(Plan, slug=slug, is_active=True)

    UserSubscription.objects.filter(user=request.user, status='active').update(status='inactive')

    UserSubscription.objects.create(
        user=request.user,
        plan=plan,
        status='active',
        expiry_date=timezone.now() + timedelta(days=plan.duration_days),
        is_trial=(plan.plan_type == 'free_trial'),
    )

    messages.success(request, f'You are now on the {plan.name} plan!')
    return redirect('subscriptions:dashboard')


@login_required
def dashboard_view(request):
    sub = UserSubscription.objects.filter(user=request.user, status='active').first()
    if not sub or not sub.is_valid:
        messages.info(request, 'Choose a plan to unlock your dashboard.')
        return redirect('subscriptions:plans')

    from posts.models import Post
    from posts.publisher import publish_due_posts

    # Publish any of this user's due scheduled posts immediately on dashboard visit
    publish_due_posts()

    posts = Post.objects.filter(user=request.user)
    stats = {
        'total': posts.count(),
        'drafts': posts.filter(status=Post.STATUS_DRAFT).count(),
        'scheduled': posts.filter(status=Post.STATUS_SCHEDULED).count(),
        'published': posts.filter(status=Post.STATUS_PUBLISHED).count(),
        'failed': posts.filter(status=Post.STATUS_FAILED).count(),
    }

    from accounts.models import UserProfile
    from posts.meta import (
        facebook_publish_ready,
        instagram_publish_ready,
        meta_configured,
    )

    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    fb_ready = facebook_publish_ready(request.user)
    ig_ready = instagram_publish_ready(request.user)

    return render(request, 'subscriptions/dashboard.html', {
        'subscription': sub,
        'posts': posts[:50],
        'stats': stats,
        'profile': profile,
        'meta_connected': profile.meta_connected,
        'facebook_ready': fb_ready,
        'instagram_ready': ig_ready,
        'meta_app_configured': meta_configured(),
    })
