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


def _apply_media_from_form(request, post, form):
    """Attach gallery uploads, library picks, and/or multi AI images as single/carousel."""
    from .media_utils import (
        attach_carousel_from_assets,
        attach_single_image_asset,
        clear_post_media,
        existing_image_assets_for_post,
        kind_from_name,
        merge_image_assets,
        parse_asset_ids,
        save_ai_temp_to_library,
        save_upload_to_library,
    )
    from .models import MediaAsset

    prompt = (form.cleaned_data.get('image_prompt') or '').strip()
    carousel_files = request.FILES.getlist('carousel_files')
    library_ids = (form.cleaned_data.get('library_asset_ids') or '').strip()
    library_assets = [
        a for a in parse_asset_ids(library_ids, request.user)
        if a.kind == MediaAsset.KIND_IMAGE
    ]
    replace_existing = (request.POST.get('replace_existing_media') or '').strip() == '1'

    uploaded_assets = []
    for f in carousel_files[:10]:
        if kind_from_name(f.name) != MediaAsset.KIND_IMAGE:
            continue
        uploaded_assets.append(save_upload_to_library(request.user, f))

    raw_paths = (form.cleaned_data.get('generated_image_paths') or '').strip()
    if not raw_paths:
        single = (form.cleaned_data.get('generated_image_path') or '').strip()
        raw_paths = single
    path_parts = [p.strip() for p in raw_paths.split(',') if p.strip()]

    ai_assets = []
    for rel in path_parts:
        full = _safe_temp_path(rel, post.user_id)
        if not full:
            continue
        try:
            ai_assets.append(save_ai_temp_to_library(request.user, full, prompt=prompt))
            full.unlink(missing_ok=True)
        except Exception:
            logger.exception('Could not save AI temp into library')

    existing_assets = existing_image_assets_for_post(post, promote_cover=False)
    existing_ids = {a.pk for a in existing_assets}

    if replace_existing:
        # Client sent the full remaining set via library / uploads / AI
        combined = merge_image_assets(uploaded_assets, library_assets, ai_assets)
    else:
        new_library = [a for a in library_assets if a.pk not in existing_ids]
        if uploaded_assets or new_library or ai_assets:
            # Append new photos to the ones already on the draft/post
            existing_assets = existing_image_assets_for_post(post, promote_cover=True)
            combined = merge_image_assets(
                existing_assets,
                uploaded_assets,
                library_assets,
                ai_assets,
            )
        else:
            combined = []

    if combined:
        if len(combined) == 1:
            attach_single_image_asset(post, combined[0])
        else:
            attach_carousel_from_assets(post, combined)
        return

    if replace_existing:
        clear_post_media(post)
        if post.image:
            post.image.delete(save=False)
            post.image = None
        post.media_type = Post.MEDIA_IMAGE
        post.save(update_fields=['image', 'media_type', 'updated_at'])
        return

    if not post.image and not post.media_items.exists():
        try:
            _attach_ai_image(post, form, force_regenerate=False)
        except ImageGenerationError:
            raise


def _save_draft_from_form(request, form, *, post=None):
    """Create or update a draft post from the form."""
    if post is None:
        post = form.save(commit=False)
        post.user = request.user
    else:
        post = form.save(commit=False)
    post.save()
    try:
        _apply_media_from_form(request, post, form)
    except ImageGenerationError:
        pass
    form.save_draft(post)
    return post


def _handle_post_submit(request, form, *, post=None):
    """Publish or schedule after validation. Returns redirect response or None."""
    if post is None:
        post = form.save(commit=False)
        post.user = request.user
    else:
        post = form.save(commit=False)

    post.save()
    try:
        _apply_media_from_form(request, post, form)
        if not post.image and not post.media_items.exists():
            _attach_ai_image(post, form, force_regenerate=post.pk is None)
            post.save()
    except ImageGenerationError as exc:
        form.add_error('image_prompt', str(exc))
        return None

    post.refresh_from_db()
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

            logger.warning('Publish failed: %s', exc)
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


def _preview_items_from_bound_form(request, form, *, post=None):
    """
    Keep selected photos visible after a failed submit.
    Gallery uploads are saved into the library so the browser file input loss
    does not wipe the user's choices.
    """
    from .media_utils import kind_from_name, parse_asset_ids, save_upload_to_library
    from .models import MediaAsset

    items = []
    seen_urls = set()

    def add_item(url, *, asset_id=0, path=''):
        if not url or url in seen_urls:
            return
        seen_urls.add(url)
        entry = {'url': url, 'asset_id': int(asset_id or 0)}
        if path:
            entry['path'] = path
        items.append(entry)

    # Start from already-saved post media when editing
    if post is not None:
        for media in post.ordered_media():
            url = media.resolve_image_url()
            if url:
                add_item(url, asset_id=media.asset_id or 0)
        if not items and post.image:
            add_item(post.image.url)

    data = request.POST.copy()
    library_raw = (data.get('library_asset_ids') or '').strip()
    library_ids = [p.strip() for p in library_raw.split(',') if p.strip()]

    # Persist gallery uploads so they survive the round-trip
    uploaded_ids = []
    for f in request.FILES.getlist('carousel_files')[:10]:
        if kind_from_name(f.name) != MediaAsset.KIND_IMAGE:
            continue
        try:
            asset = save_upload_to_library(request.user, f)
            uploaded_ids.append(str(asset.pk))
            add_item(asset.file.url, asset_id=asset.pk)
        except Exception:
            logger.exception('Could not preserve gallery upload after validation error')

    if uploaded_ids:
        for uid in uploaded_ids:
            if uid not in library_ids:
                library_ids.append(uid)
        data['library_asset_ids'] = ','.join(library_ids)
        form.data = data

    for asset in parse_asset_ids(','.join(library_ids), request.user):
        if asset.kind != MediaAsset.KIND_IMAGE or not asset.file:
            continue
        add_item(asset.file.url, asset_id=asset.pk)

    raw_paths = (data.get('generated_image_paths') or data.get('generated_image_path') or '').strip()
    for rel in [p.strip() for p in raw_paths.split(',') if p.strip()]:
        full = _safe_temp_path(rel, request.user.id)
        if not full:
            # Still show URL if file may exist under MEDIA_URL
            add_item(f'{settings.MEDIA_URL.rstrip("/")}/{rel.lstrip("/")}', path=rel)
            continue
        add_item(f'{settings.MEDIA_URL.rstrip("/")}/{rel.lstrip("/")}', path=rel)

    # If user removed existing slides in the UI, honor that for edit preview
    if post is not None and (request.POST.get('replace_existing_media') or '').strip() == '1':
        # Keep only newly chosen library/AI/upload items (already in `items`
        # after the post seed above — rebuild without post seed)
        items = []
        seen_urls = set()
        for asset in parse_asset_ids(','.join(library_ids), request.user):
            if asset.kind != MediaAsset.KIND_IMAGE or not asset.file:
                continue
            add_item(asset.file.url, asset_id=asset.pk)
        for rel in [p.strip() for p in raw_paths.split(',') if p.strip()]:
            add_item(f'{settings.MEDIA_URL.rstrip("/")}/{rel.lstrip("/")}', path=rel)

    return items


def _post_form_context(request, form, *, is_edit, post=None, page_title='Create Post', preview_items=None):
    profile = getattr(request.user, 'profile', None)
    from .media_utils import parse_asset_ids
    from .meta import facebook_publish_ready, instagram_publish_ready
    from .models import MediaAsset

    if preview_items is None:
        preview_items = []
        if post is not None:
            for item in post.ordered_media():
                url = item.resolve_image_url()
                if url:
                    preview_items.append({
                        'url': url,
                        'asset_id': item.asset_id or 0,
                    })
            if not preview_items and post.image:
                preview_items.append({'url': post.image.url, 'asset_id': 0})

    # Keep selected / restored assets visible in the picker after failed submits
    selected_ids = []
    raw_ids = ''
    if getattr(form, 'data', None):
        raw_ids = (form.data.get('library_asset_ids') or '').strip()
    selected_ids.extend(
        a.pk for a in parse_asset_ids(raw_ids, request.user) if a.kind == MediaAsset.KIND_IMAGE
    )
    for item in preview_items:
        aid = int(item.get('asset_id') or 0)
        if aid and aid not in selected_ids:
            selected_ids.append(aid)

    recent = list(
        MediaAsset.objects.filter(
            user=request.user,
            kind=MediaAsset.KIND_IMAGE,
        )[:48]
    )
    by_id = {a.pk: a for a in recent}
    for asset in MediaAsset.objects.filter(
        user=request.user,
        kind=MediaAsset.KIND_IMAGE,
        pk__in=selected_ids,
    ):
        by_id[asset.pk] = asset
    library_assets = list(by_id.values())
    library_assets.sort(key=lambda a: a.created_at, reverse=True)

    ctx = {
        'form': form,
        'subscription': request.subscription,
        'is_edit': is_edit,
        'page_title': page_title,
        'meta_connected': bool(profile and profile.meta_connected),
        'facebook_ready': facebook_publish_ready(request.user),
        'instagram_ready': instagram_publish_ready(request.user),
        'library_assets': library_assets,
    }

    if post is not None:
        ctx['post'] = post
        ctx['is_draft'] = post.status == Post.STATUS_DRAFT
    else:
        ctx['is_draft'] = False

    ctx['existing_preview_urls'] = [x['url'] for x in preview_items if x.get('url')]
    ctx['existing_preview_items'] = preview_items
    return ctx


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
        form = PostForm(request.POST, request.FILES, user=request.user, draft_mode=save_draft)
        if form.is_valid():
            if save_draft:
                _save_draft_from_form(request, form)
                messages.success(request, 'Draft saved. You can continue editing anytime.')
                return redirect(reverse('subscriptions:dashboard') + '?clear_post_draft=1&tab=draft')
            redirect_response = _handle_post_submit(request, form)
            if redirect_response:
                return redirect_response
        preview_items = _preview_items_from_bound_form(request, form)
        return render(
            request,
            'posts/post_form.html',
            _post_form_context(request, form, is_edit=False, preview_items=preview_items),
        )

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
            request.FILES,
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
        preview_items = _preview_items_from_bound_form(request, form, post=post)
        return render(
            request,
            'posts/post_form.html',
            _post_form_context(
                request,
                form,
                is_edit=True,
                post=post,
                page_title=page_title,
                preview_items=preview_items,
            ),
        )

    form = PostForm(instance=post, user=request.user)
    return render(
        request,
        'posts/post_form.html',
        _post_form_context(request, form, is_edit=True, post=post, page_title=page_title),
    )


@subscription_required
@require_POST
def post_duplicate_view(request, pk):
    post = _user_post_or_404(request, pk)
    clone = post.duplicate_for(request.user)
    messages.success(request, 'Post duplicated as a draft. Update the caption or schedule, then publish.')
    return redirect('posts:edit', pk=clone.pk)


@subscription_required
def media_library_view(request):
    from .models import MediaAsset

    assets = MediaAsset.objects.filter(user=request.user, kind=MediaAsset.KIND_IMAGE)

    return render(request, 'posts/media_library.html', {
        'subscription': request.subscription,
        'assets': assets[:120],
    })


@subscription_required
@require_POST
def media_upload_view(request):
    from .media_utils import kind_from_name, save_upload_to_library
    from .models import MediaAsset

    files = request.FILES.getlist('files') or ([request.FILES['file']] if request.FILES.get('file') else [])
    if not files:
        messages.error(request, 'Choose at least one image to upload.')
        return redirect('posts:media_library')

    saved = 0
    skipped = 0
    for f in files[:20]:
        if kind_from_name(f.name) != MediaAsset.KIND_IMAGE:
            skipped += 1
            continue
        save_upload_to_library(request.user, f)
        saved += 1
    if saved:
        messages.success(request, f'Added {saved} image{"s" if saved != 1 else ""} to your media library.')
    if skipped:
        messages.warning(request, 'Video uploads are disabled for now — only images were saved.')
    if not saved and not skipped:
        messages.error(request, 'No images were uploaded.')
    return redirect('posts:media_library')


@subscription_required
@require_POST
def media_delete_view(request, pk):
    from .models import MediaAsset

    asset = get_object_or_404(MediaAsset, pk=pk, user=request.user)
    if asset.file:
        asset.file.delete(save=False)
    asset.delete()
    messages.success(request, 'Removed from media library.')
    return redirect('posts:media_library')


@subscription_required
def post_delete_view(request, pk):
    post = _user_post_or_404(request, pk)
    was_draft = post.status == Post.STATUS_DRAFT
    was_published = post.status == Post.STATUS_PUBLISHED

    if request.method == 'POST':
        if post.image:
            post.image.delete(save=False)
        if post.video:
            post.video.delete(save=False)
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
    preview_urls = []
    for item in post.ordered_media():
        url = item.resolve_image_url()
        if url:
            preview_urls.append(url)
    if not preview_urls and post.image:
        preview_urls.append(post.image.url)
    return render(request, 'posts/post_preview.html', {
        'post': post,
        'subscription': request.subscription,
        'preview_urls': preview_urls,
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

    if not post.image and post.media_items.count() < 1:
        messages.error(request, 'This post needs an image or carousel before it can be published.')
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

            logger.warning('Platform publish failed: %s', exc)
            messages.error(request, friendly_user_error(exc))
            if post.status == Post.STATUS_SCHEDULED:
                Post.objects.filter(pk=post.pk).update(status=Post.STATUS_FAILED)

    return redirect('subscriptions:dashboard')
