from django.shortcuts import redirect, render

from subscriptions.models import UserSubscription


def home_view(request):
    if request.user.is_authenticated:
        sub = UserSubscription.objects.filter(user=request.user, status='active').first()
        if sub and sub.is_valid:
            return redirect('subscriptions:dashboard')
        return redirect('subscriptions:plans')

    features = [
        {
            'icon': '◷',
            'title': 'Smart Scheduling',
            'desc': 'Plan posts days or weeks ahead. Pick the perfect date and time for maximum engagement.',
        },
        {
            'icon': '◈',
            'title': 'Multi-Platform',
            'desc': 'Publish to Facebook and Instagram from one dashboard — no tab switching required.',
        },
        {
            'icon': '▣',
            'title': 'Rich Media Posts',
            'desc': 'Upload images and videos, write captions, and preview exactly how posts will look.',
        },
        {
            'icon': '◔',
            'title': 'Post Analytics',
            'desc': 'Track scheduled, published, and failed posts with a clean overview of your content pipeline.',
        },
        {
            'icon': '◌',
            'title': 'Smart Notifications',
            'desc': 'Get alerted when posts go live, fail to publish, or when your subscription is expiring.',
        },
        {
            'icon': '⚡',
            'title': 'Instant Publishing',
            'desc': 'Need to post now? Skip the schedule and publish immediately with one click.',
        },
    ]

    return render(request, 'core/home.html', {
        'features': features,
    })


def privacy_policy_view(request):
    return render(request, 'privacy-policy.html')

def terms_of_service_view(request):
    return render(request, 'terms-of-service.html')