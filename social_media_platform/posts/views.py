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


def _collect_media_from_form(request, form, *, user):
    """
    Persist new uploads/AI temps into the library and return a media job dict.
    Does not attach anything to the post yet (fast path for publish-now).
    """
    from .media_utils import kind_from_name, parse_asset_ids, save_ai_temp_to_library, save_upload_to_library
    from .models import MediaAsset

    prompt = (form.cleaned_data.get('image_prompt') or '').strip()
    carousel_files = request.FILES.getlist('carousel_files')
    library_ids = (form.cleaned_data.get('library_asset_ids') or '').strip()
    replace_existing = (request.POST.get('replace_existing_media') or '').strip() == '1'

    uploaded_ids = []
    for f in carousel_files[:10]:
        kind = kind_from_name(f.name)
        if kind not in {MediaAsset.KIND_IMAGE, MediaAsset.KIND_VIDEO}:
            continue
        asset = save_upload_to_library(user, f)
        uploaded_ids.append(asset.pk)

    raw_paths = (form.cleaned_data.get('generated_image_paths') or '').strip()
    if not raw_paths:
        single = (form.cleaned_data.get('generated_image_path') or '').strip()
        raw_paths = single
    path_parts = [p.strip() for p in raw_paths.split(',') if p.strip()]

    ai_ids = []
    for rel in path_parts:
        full = _safe_temp_path(rel, user.id)
        if not full:
            continue
        try:
            asset = save_ai_temp_to_library(user, full, prompt=prompt)
            ai_ids.append(asset.pk)
            full.unlink(missing_ok=True)
        except Exception:
            logger.exception('Could not save AI temp into library')

    library_picked = parse_asset_ids(library_ids, user)
    return {
        'uploaded_ids': uploaded_ids,
        'library_ids': [a.pk for a in library_picked],
        'ai_ids': ai_ids,
        'replace_existing': replace_existing,
        'prompt': prompt,
    }


def _attach_media_job(post, media_job: dict | None) -> None:
    """Attach stashed library assets onto a post (used by background publish)."""
    from .media_utils import attach_media_job

    attach_media_job(post, media_job)


def _apply_media_from_form(request, post, form):
    """Attach gallery uploads, library picks, and/or multi AI images as single/carousel/video."""
    from .media_utils import attach_media_job

    media_job = _collect_media_from_form(request, form, user=request.user)
    had_new = bool(
        media_job.get('uploaded_ids')
        or media_job.get('library_ids')
        or media_job.get('ai_ids')
        or media_job.get('replace_existing')
    )
    if had_new:
        attach_media_job(post, media_job)
        return

    if not post.image and not post.video and not post.media_items.exists():
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
    from .forms import PostForm
    from .publisher import enqueue_publish

    if post is None:
        post = form.save(commit=False)
        post.user = request.user
    else:
        post = form.save(commit=False)

    post.save()
    action = form.cleaned_data.get('publish_action')

    # Publish now: stash uploads, queue background attach+Meta publish, redirect ASAP.
    if action == PostForm.PUBLISH_NOW:
        try:
            media_job = _collect_media_from_form(request, form, user=request.user)
        except Exception as exc:
            logger.exception('Could not save media before background publish')
            form.add_error(None, f'Could not save media: {exc}')
            return None

        has_pending = bool(
            media_job.get('uploaded_ids')
            or media_job.get('library_ids')
            or media_job.get('ai_ids')
        )
        has_existing = bool(post.image or post.video or post.media_items.exists())
        if not has_pending and not has_existing:
            try:
                _attach_ai_image(post, form, force_regenerate=post.pk is None)
                post.save()
            except ImageGenerationError as exc:
                form.add_error('image_prompt', str(exc))
                return None
            media_job = None

        form.apply_publish_action(post)
        use_job = has_pending or bool(media_job.get('replace_existing'))
        enqueue_publish(post.pk, media_job=media_job if use_job else None)
        messages.success(
            request,
            'Publishing started. You can follow its progress from the dashboard.',
        )
        return redirect(reverse('subscriptions:dashboard') + '?clear_post_draft=1')

    # Schedule: attach media now so the post is complete before it is queued later.
    try:
        _apply_media_from_form(request, post, form)
        if not post.image and not post.video and not post.media_items.exists():
            _attach_ai_image(post, form, force_regenerate=post.pk is None)
            post.save()
    except ImageGenerationError as exc:
        form.add_error('image_prompt', str(exc))
        return None

    post.refresh_from_db()
    form.apply_publish_action(post)

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
    Keep selected media visible after a failed submit.
    Gallery uploads are saved into the library so the browser file input loss
    does not wipe the user's choices.
    """
    from .media_utils import kind_from_name, parse_asset_ids, save_upload_to_library
    from .models import MediaAsset

    items = []
    seen_urls = set()

    def add_item(url, *, asset_id=0, path='', media_type='image'):
        if not url or url in seen_urls:
            return
        seen_urls.add(url)
        entry = {
            'url': url,
            'asset_id': int(asset_id or 0),
            'type': media_type if media_type in {'image', 'video'} else 'image',
        }
        if path:
            entry['path'] = path
        items.append(entry)

    # Start from already-saved post media when editing
    if post is not None:
        for media in post.ordered_media():
            url = media.resolve_image_url()
            if url:
                add_item(url, asset_id=media.asset_id or 0, media_type='image')
        if not items and post.video:
            add_item(post.video.url, media_type='video')
        if not items and post.image:
            add_item(post.image.url, media_type='image')

    data = request.POST.copy()
    library_raw = (data.get('library_asset_ids') or '').strip()
    library_ids = [p.strip() for p in library_raw.split(',') if p.strip()]

    # Persist gallery uploads so they survive the round-trip
    uploaded_ids = []
    for f in request.FILES.getlist('carousel_files')[:10]:
        kind = kind_from_name(f.name)
        if kind not in {MediaAsset.KIND_IMAGE, MediaAsset.KIND_VIDEO}:
            continue
        try:
            asset = save_upload_to_library(request.user, f)
            uploaded_ids.append(str(asset.pk))
            add_item(
                asset.file.url,
                asset_id=asset.pk,
                media_type='video' if asset.kind == MediaAsset.KIND_VIDEO else 'image',
            )
        except Exception:
            logger.exception('Could not preserve gallery upload after validation error')

    if uploaded_ids:
        for uid in uploaded_ids:
            if uid not in library_ids:
                library_ids.append(uid)
        data['library_asset_ids'] = ','.join(library_ids)
        form.data = data

    for asset in parse_asset_ids(','.join(library_ids), request.user):
        if not asset.file:
            continue
        if asset.kind == MediaAsset.KIND_VIDEO:
            add_item(asset.file.url, asset_id=asset.pk, media_type='video')
        elif asset.kind == MediaAsset.KIND_IMAGE:
            add_item(asset.file.url, asset_id=asset.pk, media_type='image')

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
            if not asset.file:
                continue
            if asset.kind == MediaAsset.KIND_VIDEO:
                add_item(asset.file.url, asset_id=asset.pk, media_type='video')
            elif asset.kind == MediaAsset.KIND_IMAGE:
                add_item(asset.file.url, asset_id=asset.pk, media_type='image')
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
                        'type': 'image',
                    })
            if not preview_items and post.video:
                preview_items.append({
                    'url': post.video.url,
                    'asset_id': 0,
                    'type': 'video',
                })
            if not preview_items and post.image:
                preview_items.append({
                    'url': post.image.url,
                    'asset_id': 0,
                    'type': 'image',
                })

    # Keep selected / restored assets visible in the picker after failed submits
    selected_ids = []
    raw_ids = ''
    if getattr(form, 'data', None):
        raw_ids = (form.data.get('library_asset_ids') or '').strip()
    selected_ids.extend(
        a.pk for a in parse_asset_ids(raw_ids, request.user)
        if a.kind in {MediaAsset.KIND_IMAGE, MediaAsset.KIND_VIDEO}
    )
    for item in preview_items:
        aid = int(item.get('asset_id') or 0)
        if aid and aid not in selected_ids:
            selected_ids.append(aid)

    # Only selected assets for the form chips — full library loads in the picker modal
    selected_assets = list(
        MediaAsset.objects.filter(
            user=request.user,
            kind__in=[MediaAsset.KIND_IMAGE, MediaAsset.KIND_VIDEO],
            pk__in=selected_ids,
        )
    )
    selected_assets.sort(key=lambda a: selected_ids.index(a.pk) if a.pk in selected_ids else 0)

    ctx = {
        'form': form,
        'subscription': request.subscription,
        'is_edit': is_edit,
        'page_title': page_title,
        'meta_connected': bool(profile and profile.meta_connected),
        'facebook_ready': facebook_publish_ready(request.user),
        'instagram_ready': instagram_publish_ready(request.user),
        'library_assets': selected_assets,
        'library_assets_json': [
            {
                'id': a.pk,
                'url': a.file.url if a.file else '',
                'name': a.original_name or ('Saved video' if a.kind == MediaAsset.KIND_VIDEO else 'Saved photo'),
                'type': 'video' if a.kind == MediaAsset.KIND_VIDEO else 'image',
            }
            for a in selected_assets
            if a.file
        ],
        'has_library_photos': MediaAsset.objects.filter(
            user=request.user,
            kind__in=[MediaAsset.KIND_IMAGE, MediaAsset.KIND_VIDEO],
        ).exists(),
    }

    if post is not None:
        ctx['post'] = post
        ctx['is_draft'] = post.status == Post.STATUS_DRAFT
    else:
        ctx['is_draft'] = False

    ctx['existing_preview_urls'] = [x['url'] for x in preview_items if x.get('url')]
    ctx['existing_preview_items'] = preview_items

    photo_source = ''
    if getattr(form, 'data', None):
        photo_source = (form.data.get('photo_source') or '').strip()
    if photo_source not in {'device', 'ai', 'library'}:
        photo_source = ''
    if not photo_source and post is not None and preview_items:
        sources = set()
        for item in post.ordered_media():
            asset = getattr(item, 'asset', None)
            if asset and getattr(asset, 'source', None):
                sources.add(asset.source)
        if sources == {MediaAsset.SOURCE_AI}:
            photo_source = 'ai'
        elif preview_items:
            photo_source = 'device'
    ctx['photo_source'] = photo_source
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
def media_picker_api_view(request):
    """Paginated JSON list of saved photos for the post form picker."""
    from .models import MediaAsset

    try:
        page = max(1, int(request.GET.get('page') or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = min(48, max(12, int(request.GET.get('page_size') or 24)))
    except (TypeError, ValueError):
        page_size = 24

    q = (request.GET.get('q') or '').strip()
    qs = MediaAsset.objects.filter(
        user=request.user,
        kind__in=[MediaAsset.KIND_IMAGE, MediaAsset.KIND_VIDEO],
    )
    if q:
        qs = qs.filter(original_name__icontains=q)

    total = qs.count()
    start = (page - 1) * page_size
    end = start + page_size
    items = []
    for asset in qs[start:end]:
        if not asset.file:
            continue
        items.append({
            'id': asset.pk,
            'url': asset.file.url,
            'name': asset.original_name or (
                'Saved video' if asset.kind == MediaAsset.KIND_VIDEO else 'Saved photo'
            ),
            'source': asset.source,
            'type': 'video' if asset.kind == MediaAsset.KIND_VIDEO else 'image',
            'created': asset.created_at.strftime('%b %d, %Y') if asset.created_at else '',
        })

    has_more = end < total
    return JsonResponse({
        'ok': True,
        'items': items,
        'page': page,
        'page_size': page_size,
        'total': total,
        'has_more': has_more,
    })


@subscription_required
def media_library_view(request):
    from .models import MediaAsset

    assets = MediaAsset.objects.filter(
        user=request.user,
        kind__in=[MediaAsset.KIND_IMAGE, MediaAsset.KIND_VIDEO],
    )

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
        messages.error(request, 'Choose at least one photo or video to upload.')
        return redirect('posts:media_library')

    saved = 0
    skipped = 0
    for f in files[:20]:
        kind = kind_from_name(f.name)
        if kind not in {MediaAsset.KIND_IMAGE, MediaAsset.KIND_VIDEO}:
            skipped += 1
            continue
        save_upload_to_library(request.user, f)
        saved += 1
    if saved:
        messages.success(request, f'Added {saved} file{"s" if saved != 1 else ""} to your media library.')
    if skipped:
        messages.warning(request, 'Some files were skipped. Use JPG, PNG, WEBP, GIF, MP4, or MOV.')
    if not saved and not skipped:
        messages.error(request, 'No files were uploaded.')
    return redirect('posts:media_library')


@subscription_required
@require_POST
def media_quick_upload_view(request):
    """
    Save a picked file into the library while the user keeps editing the form,
    so submitting a post does not have to re-send the whole file.
    """
    from .media_utils import kind_from_name, save_upload_to_library
    from .models import MediaAsset

    files = request.FILES.getlist('files') or (
        [request.FILES['file']] if request.FILES.get('file') else []
    )
    if not files:
        return JsonResponse({'ok': False, 'error': 'No file received.'}, status=400)

    items = []
    skipped = 0
    for f in files[:10]:
        kind = kind_from_name(f.name)
        if kind not in {MediaAsset.KIND_IMAGE, MediaAsset.KIND_VIDEO}:
            skipped += 1
            continue
        try:
            asset = save_upload_to_library(request.user, f)
        except Exception:
            logger.exception('Quick upload failed for %s', f.name)
            return JsonResponse(
                {'ok': False, 'error': 'Could not save that file. Please try again.'},
                status=500,
            )
        items.append({
            'id': asset.pk,
            'url': asset.file.url,
            'name': asset.original_name or 'Saved media',
            'type': 'video' if asset.kind == MediaAsset.KIND_VIDEO else 'image',
        })

    if not items:
        return JsonResponse(
            {'ok': False, 'error': 'Use JPG, PNG, WEBP, GIF, MP4, or MOV files.'},
            status=400,
        )

    return JsonResponse({'ok': True, 'items': items, 'skipped': skipped})


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
    preview_items = []
    for item in post.ordered_media():
        url = item.resolve_image_url()
        if url:
            preview_items.append({'url': url, 'type': 'image'})
    if not preview_items and post.video:
        preview_items.append({'url': post.video.url, 'type': 'video'})
    if not preview_items and post.image:
        preview_items.append({'url': post.image.url, 'type': 'image'})
    return render(request, 'posts/post_preview.html', {
        'post': post,
        'subscription': request.subscription,
        'preview_items': preview_items,
        'preview_urls': [x['url'] for x in preview_items],
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

    if not post.image and not post.video and post.media_items.count() < 1:
        messages.error(request, 'This post needs a photo, carousel, or video before it can be published.')
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
