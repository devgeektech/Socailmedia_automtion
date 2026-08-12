import logging
import uuid
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.core.files import File
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .ai_images import ImageGenerationError, generate_image_file, generate_image_files
from .decorators import subscription_required
from .forms import PostForm
from .models import Post

logger = logging.getLogger(__name__)


def csrf_failure(request, reason=''):
    """Return JSON for AJAX CSRF failures instead of an HTML 403 page."""
    wants_json = (
        request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        or 'application/json' in (request.headers.get('Accept') or '').lower()
    )
    if wants_json or request.path.endswith('/generate-image/'):
        return JsonResponse(
            {
                'ok': False,
                'error': 'Session expired or security check failed. Refresh the page and try again.',
            },
            status=403,
        )
    from django.views.csrf import csrf_failure as django_csrf_failure
    return django_csrf_failure(request, reason=reason)


def _user_post_or_404(request, pk):
    return get_object_or_404(Post, pk=pk, user=request.user)


def _safe_temp_path(relative_path: str, user_id: int) -> Path | None:
    """Return absolute path only if it points at this user's AI temp file."""
    if not relative_path:
        return None
    relative_path = relative_path.replace('\\', '/').lstrip('/')
    prefix = f'posts/ai_temp/{user_id}_'
    if not relative_path.startswith(prefix):
        return None
    if '..' in relative_path:
        return None
    full = Path(settings.MEDIA_ROOT) / relative_path
    try:
        full.resolve().relative_to(Path(settings.MEDIA_ROOT).resolve())
    except ValueError:
        return None
    return full if full.is_file() else None


def _attach_ai_image_optional(post, form):
    """Attach a selected temp image to the post if available (draft save)."""
    generated_path = (form.cleaned_data.get('generated_image_path') or '').strip()
    temp_file = _safe_temp_path(generated_path, post.user_id)
    if not temp_file:
        return
    with temp_file.open('rb') as fh:
        if post.image:
            post.image.delete(save=False)
        post.image.save(temp_file.name, File(fh), save=False)
    try:
        temp_file.unlink(missing_ok=True)
    except OSError:
        pass


def _save_draft_from_form(request, form, *, post=None):
    """Create or update a draft post from the form."""
    if post is None:
        post = form.save(commit=False)
        post.user = request.user
    else:
        post = form.save(commit=False)
    _attach_ai_image_optional(post, form)
    post.save()
    form.save_draft(post)
    return post


def _post_form_context(request, form, *, is_edit, post=None, page_title='Create Post'):
    profile = getattr(request.user, 'profile', None)
    from .meta import facebook_publish_ready, instagram_publish_ready

    ctx = {
        'form': form,
        'subscription': request.subscription,
        'is_edit': is_edit,
        'page_title': page_title,
        'meta_connected': bool(profile and profile.meta_connected),
        'facebook_ready': facebook_publish_ready(request.user),
        'instagram_ready': instagram_publish_ready(request.user),
    }
    if post is not None:
        ctx['post'] = post
        ctx['is_draft'] = post.status == Post.STATUS_DRAFT
    else:
        ctx['is_draft'] = False
    return ctx


def _handle_post_submit(request, form, *, post=None):
    """Publish or schedule after validation. Returns redirect response or None."""
    if post is None:
        post = form.save(commit=False)
        post.user = request.user
    else:
        post = form.save(commit=False)

    try:
        _attach_ai_image(post, form, force_regenerate=post.pk is None)
    except ImageGenerationError as exc:
        form.add_error('image_prompt', str(exc))
        return None

    post.save()
    try:
        form.apply_publish_action(post)
    except Exception as exc:
        from .meta import MetaAPIError
        from .instagram_login import is_instagram_not_ready

        if isinstance(exc, MetaAPIError) and is_instagram_not_ready(exc):
            post.refresh_from_db()
            messages.success(request, 'Post published successfully.')
            return redirect(reverse('subscriptions:dashboard') + '?clear_post_draft=1')
        if isinstance(exc, MetaAPIError):
            from .meta import friendly_user_error

            messages.error(request, friendly_user_error(exc))
            return redirect(reverse('subscriptions:dashboard') + '?clear_post_draft=1')
        raise

    if post.status == Post.STATUS_PUBLISHED:
        messages.success(request, 'Post published successfully.')
    else:
        from django.utils import timezone as dj_tz

        when = post.scheduled_at
        if when:
            local_when = dj_tz.localtime(when)
            messages.success(
                request,
                f'Your post is scheduled for {local_when.strftime("%d %b %Y, %I:%M %p")}.',
            )
        else:
            messages.success(request, 'Your post is scheduled.')
    return redirect(reverse('subscriptions:dashboard') + '?clear_post_draft=1')


def _attach_ai_image(post, form, *, force_regenerate=False):
    """Attach generated image from temp path or call OpenAI."""
    generated_path = (form.cleaned_data.get('generated_image_path') or '').strip()
    prompt = (form.cleaned_data.get('image_prompt') or '').strip()
    regenerate = force_regenerate or form.cleaned_data.get('regenerate_image')

    temp_file = _safe_temp_path(generated_path, post.user_id)
    if temp_file and (not post.image or regenerate or not post.pk):
        with temp_file.open('rb') as fh:
            post.image.save(temp_file.name, File(fh), save=False)
        try:
            temp_file.unlink(missing_ok=True)
        except OSError:
            pass
        return

    if (not post.image) or regenerate:
        if not prompt:
            raise ImageGenerationError('Enter an image prompt to generate artwork.')
        content = generate_image_file(prompt)
        if post.image:
            post.image.delete(save=False)
        post.image.save(content.name, content, save=False)


@subscription_required
@require_POST
def generate_image_view(request):
    """AJAX: generate 3 AI images and store temp media files for user selection."""
    prompt = (request.POST.get('prompt') or '').strip()
    if not prompt:
        return JsonResponse({'ok': False, 'error': 'Enter an image prompt first.'}, status=400)

    try:
        contents = generate_image_files(prompt, count=3)
    except ImageGenerationError as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400)
    except Exception as exc:
        logger.exception('Unexpected image generation failure')
        return JsonResponse(
            {'ok': False, 'error': f'Image generation failed: {exc}'},
            status=500,
        )

    try:
        from .ai_images import MIN_IMAGE_BYTES

        temp_dir = Path(settings.MEDIA_ROOT) / 'posts' / 'ai_temp'
        temp_dir.mkdir(parents=True, exist_ok=True)

        images = []
        for content in contents:
            # Prefer underlying bytes — avoid empty File.read() edge cases
            raw = getattr(content, 'file', None)
            if raw is not None and hasattr(raw, 'getvalue'):
                data = raw.getvalue()
            else:
                if hasattr(content, 'seek'):
                    try:
                        content.seek(0)
                    except Exception:
                        pass
                data = content.read() or b''

            if len(data) < MIN_IMAGE_BYTES:
                logger.error('Refusing to save tiny AI image (%s bytes)', len(data))
                return JsonResponse(
                    {
                        'ok': False,
                        'error': (
                            'OpenAI did not return a real image (got a tiny placeholder). '
                            'Restart the server and try Generate again.'
                        ),
                    },
                    status=400,
                )

            filename = f'{request.user.id}_{uuid.uuid4().hex}.png'
            dest = temp_dir / filename
            with dest.open('wb') as out:
                out.write(data)
            logger.info('Saved OpenAI image %s (%s bytes)', filename, len(data))
            relative = f'posts/ai_temp/{filename}'.replace('\\', '/')
            images.append({
                'url': settings.MEDIA_URL + relative,
                'path': relative,
            })

        if len(images) != 3:
            return JsonResponse(
                {'ok': False, 'error': 'Expected 3 OpenAI images. Please try again.'},
                status=400,
            )
    except Exception as exc:
        logger.exception('Failed saving generated images')
        return JsonResponse(
            {'ok': False, 'error': f'Could not save generated images: {exc}'},
            status=500,
        )

    return JsonResponse({'ok': True, 'images': images, 'source': 'openai'})


@subscription_required
def post_create_view(request):
    if request.method == 'POST':
        save_draft = (request.POST.get('save_draft') or '').strip() == '1'
        form = PostForm(request.POST, user=request.user, draft_mode=save_draft)
        if form.is_valid():
            if save_draft:
                _save_draft_from_form(request, form)
                messages.success(request, 'Draft saved. You can continue editing anytime.')
                return redirect(reverse('subscriptions:dashboard') + '?clear_post_draft=1&tab=draft')
            redirect_response = _handle_post_submit(request, form)
            if redirect_response:
                return redirect_response
    else:
        form = PostForm(user=request.user)

    return render(
        request,
        'posts/post_form.html',
        _post_form_context(request, form, is_edit=False),
    )


@subscription_required
def post_edit_view(request, pk):
    post = _user_post_or_404(request, pk)
    if not post.can_edit and post.status != Post.STATUS_PUBLISHED:
        messages.error(request, 'This post cannot be edited.')
        return redirect('subscriptions:dashboard')

    is_draft = post.status == Post.STATUS_DRAFT
    page_title = 'Continue draft' if is_draft else 'Edit Post'

    if request.method == 'POST':
        save_draft = (request.POST.get('save_draft') or '').strip() == '1'
        form = PostForm(
            request.POST,
            instance=post,
            user=request.user,
            draft_mode=save_draft,
        )
        if form.is_valid():
            if save_draft:
                _save_draft_from_form(request, form, post=post)
                messages.success(request, 'Draft saved.')
                return redirect(reverse('subscriptions:dashboard') + '?clear_post_draft=1&tab=draft')
            redirect_response = _handle_post_submit(request, form, post=post)
            if redirect_response:
                return redirect_response
    else:
        form = PostForm(instance=post, user=request.user)

    return render(
        request,
        'posts/post_form.html',
        _post_form_context(request, form, is_edit=True, post=post, page_title=page_title),
    )

@subscription_required
def post_delete_view(request, pk):
    post = _user_post_or_404(request, pk)
    was_draft = post.status == Post.STATUS_DRAFT
    was_published = post.status == Post.STATUS_PUBLISHED

    if request.method == 'POST':
        if post.image:
            post.image.delete(save=False)
        post.delete()
        messages.success(
            request,
            'Draft deleted.' if was_draft else (
                'Removed from SocialFlow. If it was already posted, it stays on Facebook/Instagram.'
                if was_published else 'Post deleted.'
            ),
        )
        next_tab = (request.POST.get('next_tab') or '').strip()
        if next_tab in {'draft', 'published', 'scheduled', 'failed'}:
            return redirect(reverse('subscriptions:dashboard') + f'?tab={next_tab}')
        return redirect('subscriptions:dashboard')

    return render(request, 'posts/post_confirm_delete.html', {
        'post': post,
        'subscription': request.subscription,
    })


@subscription_required
def post_preview_view(request, pk):
    post = _user_post_or_404(request, pk)
    return render(request, 'posts/post_preview.html', {
        'post': post,
        'subscription': request.subscription,
    })


@subscription_required
@require_POST
def publish_platform_view(request, pk):
    """Publish an existing post to Facebook or Instagram with one click."""
    from .meta import (
        MetaAPIError,
        facebook_publish_ready,
        instagram_publish_ready,
        meta_configured,
        publish_post_to_meta,
    )

    post = _user_post_or_404(request, pk)
    platform = (request.POST.get('platform') or '').strip().lower()
    if platform not in {'facebook', 'instagram'}:
        messages.error(request, 'Choose Facebook or Instagram.')
        return redirect('subscriptions:dashboard')

    if not post.image:
        messages.error(request, 'This post needs an image before it can be published socially.')
        return redirect('subscriptions:dashboard')

    if platform == 'facebook' and not meta_configured():
        messages.error(request, 'Facebook is not available right now. Please try again later.')
        return redirect('subscriptions:dashboard')

    if platform == 'facebook' and not facebook_publish_ready(request.user):
        messages.error(request, 'Connect Facebook first, then try again.')
        return redirect('accounts:social_connections')
    if platform == 'instagram' and not instagram_publish_ready(request.user):
        messages.error(request, 'Connect Instagram first, then try again.')
        return redirect('accounts:social_connections')

    try:
        result = publish_post_to_meta(post, platforms={platform})
        if post.status != Post.STATUS_PUBLISHED:
            post.mark_published()
        messages.success(request, 'Post published successfully.')
    except MetaAPIError as exc:
        from .instagram_login import is_instagram_not_ready

        if is_instagram_not_ready(exc):
            if post.status != Post.STATUS_PUBLISHED:
                post.mark_published()
            messages.success(request, 'Post published successfully.')
        else:
            from .meta import friendly_user_error

            messages.error(request, friendly_user_error(exc))
            if post.status == Post.STATUS_SCHEDULED:
                Post.objects.filter(pk=post.pk).update(status=Post.STATUS_FAILED)

    return redirect('subscriptions:dashboard')
