from functools import wraps

from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect


def superadmin_required(view_func):
    """Require authenticated Django superuser for custom /admin panel."""

    @wraps(view_func)
    @login_required(login_url='dashboard:login')
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_superuser:
            return redirect('dashboard:login')
        return view_func(request, *args, **kwargs)

    return _wrapped
