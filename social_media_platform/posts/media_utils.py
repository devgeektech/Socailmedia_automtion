"""Helpers for media library attach, carousel slides, and video."""

from __future__ import annotations

from pathlib import Path

from django.core.files import File

from .models import MediaAsset, Post, PostMedia


IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.m4v'}


def kind_from_name(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in VIDEO_EXTENSIONS:
        return MediaAsset.KIND_VIDEO
    return MediaAsset.KIND_IMAGE


def save_upload_to_library(user, uploaded_file, *, source=MediaAsset.SOURCE_UPLOAD, prompt='') -> MediaAsset:
    kind = kind_from_name(uploaded_file.name)
    asset = MediaAsset(
        user=user,
        kind=kind,
        source=source,
        prompt=prompt or '',
        original_name=uploaded_file.name[:255],
    )
    asset.file.save(uploaded_file.name, uploaded_file, save=True)
    return asset


def save_ai_temp_to_library(user, temp_path: Path, *, prompt='') -> MediaAsset:
    asset = MediaAsset(
        user=user,
        kind=MediaAsset.KIND_IMAGE,
        source=MediaAsset.SOURCE_AI,
        prompt=prompt or '',
        original_name=temp_path.name[:255],
    )
    with temp_path.open('rb') as fh:
        asset.file.save(temp_path.name, File(fh), save=True)
    return asset


def parse_asset_ids(raw: str, user) -> list[MediaAsset]:
    if not raw:
        return []
    ids = []
    for part in raw.replace(' ', '').split(','):
        if part.isdigit():
            ids.append(int(part))
    if not ids:
        return []
    assets = list(
        MediaAsset.objects.filter(user=user, pk__in=ids).order_by('id')
    )
    by_id = {a.pk: a for a in assets}
    return [by_id[i] for i in ids if i in by_id]


def clear_post_media(post: Post) -> None:
    post.media_items.all().delete()


def _copy_asset_into(field, asset: MediaAsset, name: str) -> None:
    """Stream an asset file into a post FileField without buffering it in memory."""
    asset.file.open('rb')
    try:
        field.save(name, File(asset.file), save=False)
    finally:
        try:
            asset.file.close()
        except Exception:
            pass


def attach_carousel_from_assets(post: Post, assets: list[MediaAsset]) -> None:
    clear_post_media(post)
    image_assets = [a for a in assets if a.kind == MediaAsset.KIND_IMAGE]
    PostMedia.objects.bulk_create([
        PostMedia(post=post, asset=asset, order=idx)
        for idx, asset in enumerate(image_assets[:10])
    ])
    if image_assets:
        first = image_assets[0]
        # Keep Post.image as cover for dashboard/preview
        if post.image:
            post.image.delete(save=False)
        _copy_asset_into(post.image, first, f'cover_{first.pk}_{Path(first.file.name).name}')
        if post.video:
            post.video.delete(save=False)
            post.video = None
        post.media_type = Post.MEDIA_CAROUSEL if len(image_assets) >= 2 else Post.MEDIA_IMAGE
        post.save(update_fields=['image', 'video', 'media_type', 'updated_at'])


def attach_video_from_asset(post: Post, asset: MediaAsset) -> None:
    clear_post_media(post)
    if post.video:
        post.video.delete(save=False)
    _copy_asset_into(post.video, asset, f'vid_{asset.pk}_{Path(asset.file.name).name}')
    if post.image:
        post.image.delete(save=False)
        post.image = None
    post.media_type = Post.MEDIA_VIDEO
    post.save(update_fields=['video', 'image', 'media_type', 'updated_at'])


def attach_single_image_asset(post: Post, asset: MediaAsset) -> None:
    clear_post_media(post)
    if post.image:
        post.image.delete(save=False)
    _copy_asset_into(post.image, asset, f'img_{asset.pk}_{Path(asset.file.name).name}')
    if post.video:
        post.video.delete(save=False)
        post.video = None
    post.media_type = Post.MEDIA_IMAGE
    post.save(update_fields=['image', 'video', 'media_type', 'updated_at'])


def existing_image_assets_for_post(post: Post, *, promote_cover: bool = True) -> list[MediaAsset]:
    """Return image assets already on the post (PostMedia, or cover image as a library asset)."""
    assets = []
    seen = set()
    for item in post.ordered_media():
        asset = item.asset
        if not asset or asset.kind != MediaAsset.KIND_IMAGE or not asset.file:
            continue
        if asset.pk in seen:
            continue
        seen.add(asset.pk)
        assets.append(asset)
    if assets:
        return assets

    if not promote_cover or not post.image:
        return []

    # Legacy single-image posts may only have Post.image — keep it as a library asset
    name = Path(post.image.name).name or 'existing.jpg'
    asset = MediaAsset(
        user=post.user,
        kind=MediaAsset.KIND_IMAGE,
        source=MediaAsset.SOURCE_UPLOAD,
        original_name=name[:255],
    )
    post.image.open('rb')
    try:
        asset.file.save(name, File(post.image), save=True)
    finally:
        try:
            post.image.close()
        except Exception:
            pass
    return [asset]


def attach_from_temp_paths(post: Post, temp_paths: list[Path], *, user, prompt='') -> None:
    """Turn AI temp files into library assets and attach as single/carousel."""
    assets = []
    for path in temp_paths[:10]:
        if not path or not path.is_file():
            continue
        assets.append(save_ai_temp_to_library(user, path, prompt=prompt))
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    if not assets:
        return
    if len(assets) == 1:
        attach_single_image_asset(post, assets[0])
    else:
        attach_carousel_from_assets(post, assets)


def merge_image_assets(*groups: list[MediaAsset]) -> list[MediaAsset]:
    seen = set()
    merged = []
    for group in groups:
        for asset in group:
            if not asset or asset.pk in seen:
                continue
            if asset.kind != MediaAsset.KIND_IMAGE:
                continue
            seen.add(asset.pk)
            merged.append(asset)
    return merged[:10]


def first_video_asset(*groups: list[MediaAsset]) -> MediaAsset | None:
    """Return the first video asset across groups (publish allows one video)."""
    for group in groups:
        for asset in group:
            if asset and asset.kind == MediaAsset.KIND_VIDEO and asset.file:
                return asset
    return None


def clear_post_video(post: Post) -> None:
    if post.video:
        post.video.delete(save=False)
        post.video = None


def attach_media_job(post: Post, media_job: dict | None) -> None:
    """Attach stashed library assets onto a post (used by background publish)."""
    if not media_job:
        return

    uploaded_ids = [str(i) for i in (media_job.get('uploaded_ids') or [])]
    library_ids = [str(i) for i in (media_job.get('library_ids') or [])]
    ai_ids = [str(i) for i in (media_job.get('ai_ids') or [])]
    replace_existing = bool(media_job.get('replace_existing'))

    uploaded = parse_asset_ids(','.join(uploaded_ids), post.user)
    library_picked = parse_asset_ids(','.join(library_ids), post.user)
    ai_assets = parse_asset_ids(','.join(ai_ids), post.user)

    uploaded_images = [a for a in uploaded if a.kind == MediaAsset.KIND_IMAGE]
    uploaded_videos = [a for a in uploaded if a.kind == MediaAsset.KIND_VIDEO]
    library_images = [a for a in library_picked if a.kind == MediaAsset.KIND_IMAGE]
    library_videos = [a for a in library_picked if a.kind == MediaAsset.KIND_VIDEO]

    video_asset = first_video_asset(uploaded_videos, library_videos)
    has_images = bool(uploaded_images or library_images or ai_assets)
    if video_asset is not None and (uploaded_videos or not has_images):
        attach_video_from_asset(post, video_asset)
        return

    existing_assets = existing_image_assets_for_post(post, promote_cover=False)
    existing_ids = {a.pk for a in existing_assets}

    if replace_existing:
        combined = merge_image_assets(uploaded_images, library_images, ai_assets)
    else:
        new_library = [a for a in library_images if a.pk not in existing_ids]
        if uploaded_images or new_library or ai_assets:
            existing_assets = existing_image_assets_for_post(post, promote_cover=True)
            combined = merge_image_assets(
                existing_assets,
                uploaded_images,
                library_images,
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

    if video_asset is not None:
        attach_video_from_asset(post, video_asset)
        return

    if replace_existing:
        clear_post_media(post)
        clear_post_video(post)
        if post.image:
            post.image.delete(save=False)
            post.image = None
        post.media_type = Post.MEDIA_IMAGE
        post.save(update_fields=['image', 'video', 'media_type', 'updated_at'])
