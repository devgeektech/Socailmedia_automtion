from django.shortcuts import redirect, render

from subscriptions.models import UserSubscription


def home_view(request):
    if request.user.is_authenticated:
        sub = UserSubscription.objects.filter(user=request.user, status='active').first()
        if sub and sub.is_valid:
            return redirect('subscriptions:dashboard')
        return redirect('subscriptions:plans')

    promises = [
        {
            'title': 'One caption, many destinations',
            'desc': 'Write once, then send to Facebook, Instagram, or both — without rebuilding the post.',
        },
        {
            'title': 'Schedule with confidence',
            'desc': 'Pick the moment that fits your audience. SocialFlow posts when you planned it.',
        },
        {
            'title': 'Keep drafts close',
            'desc': 'Park unfinished work, come back later, and publish when it feels right.',
        },
    ]

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
            'desc': 'Describe what you want, select one or more of three options, and build a single post or carousel.',
        },
        {
            'icon': '◫',
            'title': 'Carousel posts',
            'desc': 'Mix gallery uploads, library picks, and AI images — preview as a carousel before you publish.',
        },
        {
            'icon': '▦',
            'title': 'Media library',
            'desc': 'Store and reuse previously uploaded or AI-generated images across posts.',
        },
        {
            'icon': '⧉',
            'title': 'Post duplication',
            'desc': 'Duplicate an existing post, then change the caption, date, or media.',
        },
        {
            'icon': '⌕',
            'title': 'Search & filters',
            'desc': 'Find posts by caption, status, platform, and date — fast.',
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

    pipeline = [
        {
            'title': 'Draft',
            'desc': 'Capture the idea before it’s ready for the world.',
        },
        {
            'title': 'Scheduled',
            'desc': 'Lock a time and let SocialFlow handle the send.',
        },
        {
            'title': 'Published',
            'desc': 'Confirm it went live across your connected channels.',
        },
        {
            'title': 'Failed',
            'desc': 'Spot issues quickly and retry without starting over.',
        },
    ]

    personas = [
        {
            'title': 'Creators',
            'desc': 'Keep a steady posting rhythm without living inside Meta’s apps all day.',
        },
        {
            'title': 'Small brands',
            'desc': 'Manage Page + Instagram publishing from one calm dashboard.',
        },
        {
            'title': 'Solo marketers',
            'desc': 'Draft, schedule, and ship content without a heavy social suite.',
        },
    ]

    return render(request, 'core/home.html', {
        'promises': promises,
        'features': features,
        'pipeline': pipeline,
        'personas': personas,
    })


def privacy_policy_view(request):
    return render(request, 'privacy-policy.html')


def terms_of_service_view(request):
    return render(request, 'terms-of-service.html')
