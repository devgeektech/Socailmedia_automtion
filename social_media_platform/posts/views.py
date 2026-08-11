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
        form = PostForm(request.POST, user=request.user)
        if form.is_valid():
            post = form.save(commit=False)
            post.user = request.user
            try:
                _attach_ai_image(post, form, force_regenerate=True)
            except ImageGenerationError as exc:
                form.add_error('image_prompt', str(exc))
            else:
                post.save()
                try:
                    form.apply_publish_action(post)
                except Exception as exc:
                    from .meta import MetaAPIError
                    if isinstance(exc, MetaAPIError):
                        messages.error(request, str(exc))
                        return redirect(reverse('subscriptions:dashboard') + '?clear_post_draft=1')
                    raise
                if post.status == Post.STATUS_PUBLISHED:
                    posted = []
                    if post.facebook_post_id:
                        posted.append('Facebook')
                    if post.instagram_media_id:
                        posted.append('Instagram')
                    if posted:
                        messages.success(request, f'Posted on {" and ".join(posted)}.')
                    else:
                        messages.success(request, 'Post published.')
                else:
                    from django.utils import timezone as dj_tz

                    platforms = []
                    if post.publish_to_facebook:
                        platforms.append('Facebook')
                    if post.publish_to_instagram:
                        platforms.append('Instagram')
                    when = post.scheduled_at
                    if platforms and when:
                        local_when = dj_tz.localtime(when)
                        messages.success(
                            request,
                            f'Scheduled for {" & ".join(platforms)} at '
                            f'{local_when.strftime("%d %b %Y, %I:%M %p")}. '
                            'It will post automatically — no extra click needed.',
                        )
                    else:
                        messages.success(request, 'Post scheduled successfully.')
                return redirect(reverse('subscriptions:dashboard') + '?clear_post_draft=1')
    else:
        form = PostForm(user=request.user)

    profile = getattr(request.user, 'profile', None)
    from .meta import facebook_publish_ready, instagram_publish_ready

    return render(request, 'posts/post_form.html', {
        'form': form,
        'subscription': request.subscription,
        'is_edit': False,
        'page_title': 'Create Post',
        'meta_connected': bool(profile and profile.meta_connected),
        'facebook_ready': facebook_publish_ready(request.user),
        'instagram_ready': instagram_publish_ready(request.user),
    })


@subscription_required
def post_edit_view(request, pk):
    post = _user_post_or_404(request, pk)
    if not post.can_edit and post.status != Post.STATUS_PUBLISHED:
        messages.error(request, 'This post cannot be edited.')
        return redirect('subscriptions:dashboard')

    if request.method == 'POST':
        form = PostForm(request.POST, instance=post, user=request.user)
        if form.is_valid():
            post = form.save(commit=False)
            try:
                _attach_ai_image(post, form)
            except ImageGenerationError as exc:
                form.add_error('image_prompt', str(exc))
            else:
                post.save()
                try:
                    form.apply_publish_action(post)
                except Exception as exc:
                    from .meta import MetaAPIError
                    if isinstance(exc, MetaAPIError):
                        messages.error(request, str(exc))
                        return redirect(reverse('subscriptions:dashboard') + '?clear_post_draft=1')
                    raise
                messages.success(request, 'Post updated successfully.')
                return redirect(reverse('subscriptions:dashboard') + '?clear_post_draft=1')
    else:
        form = PostForm(instance=post, user=request.user)

    profile = getattr(request.user, 'profile', None)
    from .meta import facebook_publish_ready, instagram_publish_ready

    return render(request, 'posts/post_form.html', {
        'form': form,
        'post': post,
        'subscription': request.subscription,
        'is_edit': True,
        'page_title': 'Edit Post',
        'meta_connected': bool(profile and profile.meta_connected),
        'facebook_ready': facebook_publish_ready(request.user),
        'instagram_ready': instagram_publish_ready(request.user),
    })

@subscription_required
def post_delete_view(request, pk):
    post = _user_post_or_404(request, pk)

    if request.method == 'POST':
        if post.image:
            post.image.delete(save=False)
        post.delete()
        messages.success(request, 'Post deleted.')
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
        messages.error(
            request,
            'Facebook Login is not configured. Ask the admin to set META_APP_ID and META_APP_SECRET.',
        )
        return redirect('subscriptions:dashboard')

    if platform == 'facebook' and not facebook_publish_ready(request.user):
        messages.error(request, 'Connect Facebook from Social Connections first.')
        return redirect('accounts:social_connections')
    if platform == 'instagram' and not instagram_publish_ready(request.user):
        messages.error(request, 'Connect Instagram from Social Connections first.')
        return redirect('accounts:social_connections')

    try:
        result = publish_post_to_meta(post, platforms={platform})
        if post.status != Post.STATUS_PUBLISHED:
            post.mark_published()
        if platform == 'facebook':
            messages.success(request, 'Posted on Facebook.')
        else:
            messages.success(request, 'Posted on Instagram.')
    except MetaAPIError as exc:
        messages.error(request, str(exc))
        if post.status == Post.STATUS_SCHEDULED:
            Post.objects.filter(pk=post.pk).update(status=Post.STATUS_FAILED)

    return redirect('subscriptions:dashboard')
