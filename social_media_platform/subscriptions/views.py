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

    from django.db.models import Q
    from django.utils.dateparse import parse_date

    from posts.models import Post
    from posts.publisher import publish_due_posts

    # Publish any of this user's due scheduled posts immediately on dashboard visit
    publish_due_posts()

    posts = Post.objects.filter(user=request.user)
    stats = {
        'total': posts.count(),
        'drafts': posts.filter(status=Post.STATUS_DRAFT).count(),
        'publishing': posts.filter(status=Post.STATUS_PUBLISHING).count(),
        'scheduled': posts.filter(status=Post.STATUS_SCHEDULED).count(),
        'published': posts.filter(status=Post.STATUS_PUBLISHED).count(),
        'failed': posts.filter(status=Post.STATUS_FAILED).count(),
    }

    q = (request.GET.get('q') or '').strip()
    status = (request.GET.get('status') or request.GET.get('tab') or '').strip()
    if status == 'all':
        status = ''
    platform = (request.GET.get('platform') or '').strip().lower()
    date_from = parse_date((request.GET.get('date_from') or '').strip() or '')
    date_to = parse_date((request.GET.get('date_to') or '').strip() or '')

    filtered = posts
    if q:
        filtered = filtered.filter(
            Q(caption__icontains=q)
            | Q(description__icontains=q)
            | Q(image_prompt__icontains=q)
        )
    if status in {
        Post.STATUS_DRAFT,
        Post.STATUS_PUBLISHING,
        Post.STATUS_SCHEDULED,
        Post.STATUS_PUBLISHED,
        Post.STATUS_FAILED,
    }:
        filtered = filtered.filter(status=status)
    if platform == 'facebook':
        filtered = filtered.filter(
            Q(publish_to_facebook=True) | Q(facebook_post_id__gt='')
        )
    elif platform == 'instagram':
        filtered = filtered.filter(
            Q(publish_to_instagram=True) | Q(instagram_media_id__gt='')
        )
    if date_from:
        filtered = filtered.filter(created_at__date__gte=date_from)
    if date_to:
        filtered = filtered.filter(created_at__date__lte=date_to)

    from accounts.models import UserProfile
    from posts.meta import (
        facebook_publish_ready,
        instagram_publish_ready,
        meta_configured,
    )

    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    fb_ready = facebook_publish_ready(request.user)
    ig_ready = instagram_publish_ready(request.user)

    filters = {
        'q': q,
        'status': status,
        'platform': platform,
        'date_from': request.GET.get('date_from') or '',
        'date_to': request.GET.get('date_to') or '',
    }
    ctx = {
        'subscription': sub,
        'posts': filtered.prefetch_related('media_items__asset')[:50],
        'stats': stats,
        'profile': profile,
        'meta_connected': profile.meta_connected,
        'facebook_ready': fb_ready,
        'instagram_ready': ig_ready,
        'meta_app_configured': meta_configured(),
        'filters': filters,
        'filters_active': bool(q or platform or filters['date_from'] or filters['date_to']),
    }
    wants_partial = (
        request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        or (request.GET.get('partial') or '') == '1'
    )
    if wants_partial:
        return render(request, 'subscriptions/_dashboard_posts.html', ctx)
    return render(request, 'subscriptions/dashboard.html', ctx)
