"""Helpers for media library attach, carousel slides, and video."""

from __future__ import annotations

from pathlib import Path

from django.core.files import File
from django.core.files.base import ContentFile

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


def attach_carousel_from_assets(post: Post, assets: list[MediaAsset]) -> None:
    clear_post_media(post)
    image_assets = [a for a in assets if a.kind == MediaAsset.KIND_IMAGE]
    for idx, asset in enumerate(image_assets[:10]):
        PostMedia.objects.create(post=post, asset=asset, order=idx)
    if image_assets:
        first = image_assets[0]
        # Keep Post.image as cover for dashboard/preview
        first.file.open('rb')
        data = first.file.read()
        first.file.close()
        if post.image:
            post.image.delete(save=False)
        post.image.save(f'cover_{first.pk}_{Path(first.file.name).name}', ContentFile(data), save=False)
        if post.video:
            post.video.delete(save=False)
            post.video = None
        post.media_type = Post.MEDIA_CAROUSEL if len(image_assets) >= 2 else Post.MEDIA_IMAGE
        post.save(update_fields=['image', 'video', 'media_type', 'updated_at'])


def attach_video_from_asset(post: Post, asset: MediaAsset) -> None:
    clear_post_media(post)
    asset.file.open('rb')
    data = asset.file.read()
    asset.file.close()
    if post.video:
        post.video.delete(save=False)
    post.video.save(f'vid_{asset.pk}_{Path(asset.file.name).name}', ContentFile(data), save=False)
    if post.image:
        post.image.delete(save=False)
        post.image = None
    post.media_type = Post.MEDIA_VIDEO
    post.save(update_fields=['video', 'image', 'media_type', 'updated_at'])


def attach_single_image_asset(post: Post, asset: MediaAsset) -> None:
    clear_post_media(post)
    asset.file.open('rb')
    data = asset.file.read()
    asset.file.close()
    if post.image:
        post.image.delete(save=False)
    post.image.save(f'img_{asset.pk}_{Path(asset.file.name).name}', ContentFile(data), save=False)
    if post.video:
        post.video.delete(save=False)
        post.video = None
    post.media_type = Post.MEDIA_IMAGE
    post.save(update_fields=['image', 'video', 'media_type', 'updated_at'])


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
