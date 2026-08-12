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
            'title': 'Smart scheduling',
            'desc': 'Plan days or weeks ahead. Set the time once — SocialFlow posts when you meant it to.',
        },
        {
            'icon': '◈',
            'title': 'Facebook & Instagram',
            'desc': 'Connect both accounts and publish from one place. No more tab hopping.',
        },
        {
            'icon': '▣',
            'title': 'AI image options',
            'desc': 'Describe what you want, pick from three options, and lock in the look that fits.',
        },
        {
            'icon': '◔',
            'title': 'Clear pipeline',
            'desc': 'Drafts, scheduled, published, and failed — see everything in one dashboard.',
        },
        {
            'icon': '◌',
            'title': 'Save & resume',
            'desc': 'Park a draft, come back later, and finish when inspiration returns.',
        },
        {
            'icon': '⚡',
            'title': 'Instant publish',
            'desc': 'When the moment is now, skip the schedule and go live in one click.',
        },
    ]

    return render(request, 'core/home.html', {
        'features': features,
    })


def privacy_policy_view(request):
    return render(request, 'privacy-policy.html')

def terms_of_service_view(request):
    return render(request, 'terms-of-service.html')