from functools import wraps

from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.http import JsonResponse
from django.shortcuts import redirect

from subscriptions.models import UserSubscription


def _wants_json(request) -> bool:
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return True
    accept = (request.headers.get('Accept') or '').lower()
    return 'application/json' in accept


def subscription_required(view_func):
    """Require login plus an active, unexpired subscription."""

    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            if _wants_json(request):
                return JsonResponse(
                    {'ok': False, 'error': 'Please sign in again to generate images.'},
                    status=401,
                )
            return redirect_to_login(request.get_full_path())

        sub = UserSubscription.objects.filter(user=request.user, status='active').first()
        if not sub or not sub.is_valid:
            if _wants_json(request):
                return JsonResponse(
                    {'ok': False, 'error': 'Choose a plan to unlock image generation.'},
                    status=403,
                )
            messages.info(request, 'Choose a plan to unlock your dashboard.')
            return redirect('subscriptions:plans')
        request.subscription = sub
        return view_func(request, *args, **kwargs)

    return wrapper
