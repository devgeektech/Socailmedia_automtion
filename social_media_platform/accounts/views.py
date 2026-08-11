from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, PasswordResetConfirmView, PasswordResetView
from django.core.mail import BadHeaderError
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.crypto import get_random_string
from django.views.decorators.http import require_POST, require_http_methods
from smtplib import SMTPException
import logging

from subscriptions.models import UserSubscription

from .forms import EmailLoginForm, SignUpForm
from .models import UserProfile

logger = logging.getLogger(__name__)


def _post_auth_redirect(user):
    if user.is_superuser:
        return reverse('dashboard:home')
    sub = UserSubscription.objects.filter(user=user, status='active').first()
    if sub and sub.is_valid:
        return reverse('subscriptions:dashboard')
    return reverse('subscriptions:plans')


class CustomLoginView(LoginView):
    template_name = 'accounts/login.html'
    authentication_form = EmailLoginForm
    redirect_authenticated_user = True

    def get_success_url(self):
        return _post_auth_redirect(self.request.user)


def signup_view(request):
    if request.user.is_authenticated:
        return redirect(_post_auth_redirect(request.user))

    if request.method == 'POST':
        form = SignUpForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            messages.success(request, 'Account created! Choose your plan to get started.')
            return redirect('subscriptions:plans')
    else:
        form = SignUpForm()

    return render(request, 'accounts/signup.html', {'form': form})


class CustomPasswordResetView(PasswordResetView):
    template_name = 'accounts/forgot_password.html'
    email_template_name = 'accounts/password_reset_email.html'
    html_email_template_name = 'accounts/password_reset_email_html.html'
    subject_template_name = 'accounts/password_reset_subject.txt'
    success_url = reverse_lazy('accounts:password_reset_done')
    from_email = None  # uses DEFAULT_FROM_EMAIL

    def form_valid(self, form):
        try:
            response = super().form_valid(form)
        except (SMTPException, BadHeaderError, OSError) as exc:
            logger.exception('Password reset email failed: %s', exc)
            messages.error(
                self.request,
                'We could not send the reset email right now. Please try again shortly.',
            )
            return self.form_invalid(form)

        messages.info(
            self.request,
            'If an account exists for that email, a reset link has been sent.',
        )
        return response


class CustomPasswordResetConfirmView(PasswordResetConfirmView):
    template_name = 'accounts/reset_password.html'
    success_url = reverse_lazy('accounts:password_reset_complete')

    def form_valid(self, form):
        messages.success(self.request, 'Your password has been updated. You can sign in now.')
        return super().form_valid(form)


def _ensure_profile(user) -> UserProfile:
    profile, _ = UserProfile.objects.get_or_create(user=user)
    return profile


def _oauth_redirect_uri(request) -> str:
    """
    Must match the host where the user has their session cookie.
    Do not override with PUBLIC_BASE_URL (that is for Instagram image fetch only).
    """
    return request.build_absolute_uri(reverse('accounts:meta_callback'))


def _instagram_oauth_redirect_uri(request) -> str:
    return request.build_absolute_uri(reverse('accounts:instagram_callback'))


def _store_meta_next(request) -> None:
    """Remember where to return after Meta connect (e.g. create post form)."""
    if 'next' not in request.GET:
        return
    nxt = (request.GET.get('next') or '').strip()
    if nxt.startswith('/') and not nxt.startswith('//'):
        request.session['meta_oauth_next'] = nxt
    else:
        request.session.pop('meta_oauth_next', None)


def _meta_return_redirect(request):
    """Redirect back to create-post (or social connections) after Meta OAuth."""
    nxt = request.session.pop('meta_oauth_next', None)
    if nxt and isinstance(nxt, str) and nxt.startswith('/') and not nxt.startswith('//'):
        return redirect(nxt)
    return redirect('accounts:social_connections')


def _finish_page_selection(request, *, pages: list[dict], purpose: str, fb_user_id: str, user_token: str):
    """Auto-pick one page or send the user to the page picker."""
    from posts.meta import MetaAPIError, serialize_pages_for_session

    require_ig = purpose == 'instagram'
    if require_ig:
        pages = [
            p for p in pages
            if (p.get('instagram_business_account') or {}).get('id')
            or p.get('instagram_id')
        ]
        if not pages:
            raise MetaAPIError(
                'No Facebook Page with a linked Instagram Business account was found. '
                'In Meta Business Suite, link Instagram (Business/Creator) to your Page, '
                'then Connect Instagram again and approve all Instagram permissions.'
            )

    serialized = serialize_pages_for_session(pages)
    if len(serialized) == 1:
        profile = _ensure_profile(request.user)
        profile.apply_page_connection(
            page=serialized[0],
            fb_user_id=fb_user_id,
            user_token=user_token,
        )
        _success_messages(request, profile, purpose=purpose)
        return _meta_return_redirect(request)

    request.session['meta_pending_pages'] = serialized
    request.session['meta_pending_fb_user_id'] = fb_user_id
    request.session['meta_pending_user_token'] = user_token
    request.session['meta_oauth_purpose'] = purpose
    return redirect('accounts:meta_select_page')


def _success_messages(request, profile: UserProfile, *, purpose: str):
    page_name = profile.facebook_page_name or 'Facebook Page'
    if purpose == 'instagram' or profile.instagram_ready:
        ig_label = profile.instagram_username or 'Instagram'
        messages.success(
            request,
            f'Connected Facebook ({page_name}) and Instagram (@{ig_label}).',
        )
    else:
        messages.success(request, f'Connected Facebook ({page_name}).')


@login_required
def social_connections_view(request):
    """User-facing Facebook / Instagram connection status."""
    from posts.instagram_login import instagram_login_configured
    from posts.meta import meta_configured

    if 'next' in request.GET:
        _store_meta_next(request)
    else:
        request.session.pop('meta_oauth_next', None)
    nxt = request.session.get('meta_oauth_next') or ''
    next_q = f'&next={nxt}' if nxt else ''
    ig_next_query = f'?next={nxt}' if nxt else ''

    profile = _ensure_profile(request.user)
    return render(request, 'accounts/social_connections.html', {
        'profile': profile,
        'meta_app_configured': meta_configured(),
        'instagram_app_configured': instagram_login_configured(),
        'facebook_ready': profile.facebook_ready,
        'instagram_ready': profile.instagram_ready,
        'oauth_redirect_uri': _oauth_redirect_uri(request),
        'instagram_oauth_redirect_uri': _instagram_oauth_redirect_uri(request),
        'next_query': next_q,
        'ig_next_query': ig_next_query,
        'return_next': nxt,
    })


@login_required
def meta_connect_view(request):
    """Start Facebook Login OAuth. ?for=facebook|instagram"""
    from posts.meta import (
        MetaAPIError,
        fetch_managed_pages,
        fetch_page_instagram,
        meta_configured,
        oauth_authorize_url,
        pages_with_instagram,
    )

    purpose = (request.GET.get('for') or 'facebook').strip().lower()
    if purpose not in {'facebook', 'instagram'}:
        purpose = 'facebook'

    _store_meta_next(request)

    # Instagram uses its own Login flow — do not start Facebook OAuth.
    if purpose == 'instagram':
        return redirect('accounts:instagram_connect')

    if not meta_configured():
        messages.error(
            request,
            'Facebook is not available right now. Please try again later.',
        )
        return _meta_return_redirect(request)

    profile = _ensure_profile(request.user)

    # Fast path: Facebook Page already connected — look up IG on that Page
    if purpose == 'instagram' and profile.meta_connected:
        ig = fetch_page_instagram(
            page_id=profile.facebook_page_id,
            page_token=profile.facebook_page_access_token,
        )
        if ig:
            profile.instagram_business_account_id = ig['id']
            profile.instagram_username = ig.get('username') or ''
            profile.save(update_fields=[
                'instagram_business_account_id',
                'instagram_username',
            ])
            label = profile.instagram_username or 'Instagram'
            messages.success(request, f'Connected Instagram (@{label}).')
            return _meta_return_redirect(request)

        # Try other Pages via stored user token before full OAuth
        if profile.facebook_user_access_token:
            try:
                pages = fetch_managed_pages(profile.facebook_user_access_token)
                ig_pages = pages_with_instagram(pages)
                if ig_pages:
                    return _finish_page_selection(
                        request,
                        pages=ig_pages,
                        purpose='instagram',
                        fb_user_id=profile.facebook_user_id,
                        user_token=profile.facebook_user_access_token,
                    )
            except MetaAPIError:
                messages.warning(request, 'Please approve Instagram access on the next screen.')
            except Exception:
                logger.exception('Instagram reconnect via stored user token failed')

        # Full OAuth with rerequest so IG permissions are granted
        messages.info(request, 'Please approve Instagram access on the next screen.')

    redirect_uri = _oauth_redirect_uri(request)
    state = get_random_string(32)
    request.session['meta_oauth_state'] = state
    request.session['meta_oauth_purpose'] = purpose
    request.session['meta_oauth_redirect_uri'] = redirect_uri
    try:
        url = oauth_authorize_url(
            redirect_uri=redirect_uri,
            state=state,
            rerequest=(purpose == 'instagram'),
        )
    except MetaAPIError:
        messages.error(request, 'Could not connect Facebook. Please try again.')
        return _meta_return_redirect(request)
    return redirect(url)


@login_required
def meta_callback_view(request):
    """OAuth redirect: exchange code, then auto-save or show page picker."""
    from posts.meta import (
        MetaAPIError,
        exchange_code_for_token,
        exchange_long_lived_user_token,
        fetch_facebook_user_id,
        fetch_managed_pages,
    )

    purpose = request.session.get('meta_oauth_purpose') or 'facebook'

    error = request.GET.get('error_description') or request.GET.get('error')
    if error:
        messages.error(request, 'Facebook connection was cancelled. Please try again.')
        return _meta_return_redirect(request)

    state = request.GET.get('state')
    expected = request.session.pop('meta_oauth_state', None)
    if not state or not expected or state != expected:
        messages.error(request, 'Could not connect Facebook. Please try again.')
        return _meta_return_redirect(request)

    code = request.GET.get('code')
    if not code:
        messages.error(request, 'Could not connect Facebook. Please try again.')
        return _meta_return_redirect(request)

    # Must use the same redirect_uri that started OAuth (stored in session)
    redirect_uri = request.session.pop('meta_oauth_redirect_uri', None) or _oauth_redirect_uri(request)

    try:
        short_token = exchange_code_for_token(code=code, redirect_uri=redirect_uri)
        long_token = exchange_long_lived_user_token(short_token)
        fb_user_id = fetch_facebook_user_id(long_token)
        pages = fetch_managed_pages(long_token)
        return _finish_page_selection(
            request,
            pages=pages,
            purpose=purpose,
            fb_user_id=fb_user_id,
            user_token=long_token,
        )
    except MetaAPIError:
        messages.error(request, 'Could not connect Facebook. Please try again.')
        return _meta_return_redirect(request)
    except Exception:
        logger.exception('Meta OAuth callback failed')
        messages.error(request, 'Could not connect Facebook. Please try again.')
        return _meta_return_redirect(request)


@login_required
@require_http_methods(['GET', 'POST'])
def meta_select_page_view(request):
    """Let the user pick which Facebook Page (and linked IG) to connect."""
    pages = request.session.get('meta_pending_pages') or []
    purpose = request.session.get('meta_oauth_purpose') or 'facebook'
    if not pages:
        messages.error(request, 'Please connect Facebook again.')
        return _meta_return_redirect(request)

    if request.method == 'POST':
        page_id = (request.POST.get('page_id') or '').strip()
        from posts.meta import find_serialized_page

        page = find_serialized_page(pages, page_id)
        if not page:
            messages.error(request, 'Choose a valid Facebook Page.')
            return redirect('accounts:meta_select_page')
        if purpose == 'instagram' and not page.get('instagram_id'):
            messages.error(request, 'Please choose a Facebook Page that has Instagram connected.')
            return redirect('accounts:meta_select_page')

        profile = _ensure_profile(request.user)
        profile.apply_page_connection(
            page=page,
            fb_user_id=request.session.pop('meta_pending_fb_user_id', ''),
            user_token=request.session.pop('meta_pending_user_token', ''),
        )
        request.session.pop('meta_pending_pages', None)
        request.session.pop('meta_oauth_purpose', None)
        _success_messages(request, profile, purpose=purpose)
        return _meta_return_redirect(request)

    return render(request, 'accounts/meta_select_page.html', {
        'pages': pages,
        'purpose': purpose,
        'require_instagram': purpose == 'instagram',
    })


@login_required
def instagram_connect_view(request):
    """Start Instagram Login OAuth (no Facebook Page)."""
    from posts.instagram_login import instagram_login_configured, instagram_oauth_authorize_url
    from posts.meta import MetaAPIError

    _store_meta_next(request)

    if not instagram_login_configured():
        messages.error(
            request,
            'Instagram is not available right now. Please try again later.',
        )
        return _meta_return_redirect(request)

    redirect_uri = _instagram_oauth_redirect_uri(request)
    state = get_random_string(32)
    request.session['ig_oauth_state'] = state
    request.session['ig_oauth_redirect_uri'] = redirect_uri
    try:
        url = instagram_oauth_authorize_url(redirect_uri=redirect_uri, state=state)
    except MetaAPIError:
        messages.error(request, 'Could not connect Instagram. Please try again.')
        return _meta_return_redirect(request)
    return redirect(url)


@login_required
def instagram_callback_view(request):
    """Instagram Login OAuth redirect."""
    from posts.instagram_login import (
        exchange_instagram_code,
        exchange_long_lived_instagram_token,
        fetch_instagram_login_profile,
    )
    from posts.meta import MetaAPIError

    error = request.GET.get('error_description') or request.GET.get('error')
    if error:
        messages.error(request, 'Instagram connection was cancelled. Please try again.')
        return _meta_return_redirect(request)

    state = request.GET.get('state')
    expected = request.session.pop('ig_oauth_state', None)
    if not state or not expected or state != expected:
        messages.error(request, 'Could not connect Instagram. Please try again.')
        return _meta_return_redirect(request)

    code = request.GET.get('code')
    if not code:
        messages.error(request, 'Could not connect Instagram. Please try again.')
        return _meta_return_redirect(request)

    redirect_uri = request.session.pop('ig_oauth_redirect_uri', None) or _instagram_oauth_redirect_uri(request)

    try:
        short_token, user_id = exchange_instagram_code(code=code, redirect_uri=redirect_uri)
        long_token, expires_at = exchange_long_lived_instagram_token(short_token)
        profile_data = fetch_instagram_login_profile(long_token)
        ig_user_id = profile_data.get('user_id') or user_id
        username = profile_data.get('username') or ''
        profile = _ensure_profile(request.user)
        profile.apply_instagram_login(
            user_id=ig_user_id,
            access_token=long_token,
            username=username,
            expires_at=expires_at,
        )
        label = username or 'Instagram'
        messages.success(request, f'Connected Instagram (@{label}).')
        return _meta_return_redirect(request)
    except MetaAPIError:
        messages.error(request, 'Could not connect Instagram. Please try again.')
        return _meta_return_redirect(request)
    except Exception:
        logger.exception('Instagram Login callback failed')
        messages.error(request, 'Could not connect Instagram. Please try again.')
        return _meta_return_redirect(request)


@login_required
@require_POST
def meta_disconnect_view(request):
    profile = _ensure_profile(request.user)
    target = (request.POST.get('target') or 'all').strip().lower()
    if target == 'instagram':
        profile.clear_instagram_connection()
        messages.success(request, 'Instagram disconnected.')
    else:
        profile.clear_meta_connection()
        messages.success(request, 'Facebook disconnected.')
    return redirect('accounts:social_connections')
