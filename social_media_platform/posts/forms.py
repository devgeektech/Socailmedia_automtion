from datetime import timedelta

from django import forms
from django.utils import timezone

from .models import Post

# Large uploads can take minutes, so the chosen schedule time may already be in
# the past by the time the request finishes. Accept it instead of failing.
SCHEDULE_GRACE = timedelta(minutes=20)


class PostForm(forms.ModelForm):
    PUBLISH_NOW = 'publish_now'
    SCHEDULE = 'schedule'

    ACTION_CHOICES = [
        (PUBLISH_NOW, 'Publish immediately'),
        (SCHEDULE, 'Schedule for later'),
    ]

    publish_action = forms.ChoiceField(
        choices=ACTION_CHOICES,
        widget=forms.RadioSelect,
        initial=PUBLISH_NOW,
        label='Publishing',
    )
    scheduled_at = forms.DateTimeField(
        required=False,
        widget=forms.DateTimeInput(
            attrs={'type': 'datetime-local', 'class': 'glass-input'},
            format='%Y-%m-%dT%H:%M',
        ),
        input_formats=['%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'],
        label='Schedule date & time (Asia/Kolkata)',
    )
    regenerate_image = forms.BooleanField(
        required=False,
        initial=False,
        label='Regenerate AI image with this prompt',
    )
    generated_image_path = forms.CharField(
        required=False,
        widget=forms.HiddenInput(attrs={'id': 'id_generated_image_path'}),
    )
    # Comma-separated AI temp paths for multi-select / carousel
    generated_image_paths = forms.CharField(
        required=False,
        widget=forms.HiddenInput(attrs={'id': 'id_generated_image_paths'}),
    )
    library_asset_ids = forms.CharField(
        required=False,
        widget=forms.HiddenInput(attrs={'id': 'id_library_asset_ids'}),
    )

    class Meta:
        model = Post
        fields = [
            'description',
            'caption',
            'image_prompt',
            'publish_to_facebook',
            'publish_to_instagram',
        ]
        widgets = {
            'description': forms.Textarea(attrs={
                'class': 'glass-input',
                'rows': 5,
                'placeholder': 'Write your post description…',
                'id': 'id_description',
            }),
            'caption': forms.Textarea(attrs={
                'class': 'glass-input',
                'rows': 2,
                'placeholder': 'Add a short caption…',
                'id': 'id_caption',
            }),
            'image_prompt': forms.Textarea(attrs={
                'class': 'glass-input',
                'rows': 3,
                'placeholder': 'Describe the image you want AI to create…',
                'id': 'id_image_prompt',
            }),
            'publish_to_facebook': forms.CheckboxInput(attrs={'class': 'platform-check'}),
            'publish_to_instagram': forms.CheckboxInput(attrs={'class': 'platform-check'}),
        }
        labels = {
            'description': 'Description',
            'caption': 'Caption',
            'image_prompt': 'AI image prompt',
            'publish_to_facebook': 'Facebook',
            'publish_to_instagram': 'Instagram',
        }

    def __init__(self, *args, user=None, draft_mode=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.draft_mode = draft_mode
        instance = kwargs.get('instance')
        self.fields['image_prompt'].required = False

        if instance and instance.scheduled_at and instance.status == Post.STATUS_SCHEDULED:
            self.fields['publish_action'].initial = self.SCHEDULE
            local = timezone.localtime(instance.scheduled_at)
            self.fields['scheduled_at'].initial = local.strftime('%Y-%m-%dT%H:%M')
        elif instance and instance.status == Post.STATUS_PUBLISHED:
            self.fields['publish_action'].initial = self.PUBLISH_NOW

        from .meta import facebook_publish_ready, instagram_publish_ready

        fb_ready = bool(user and facebook_publish_ready(user))
        ig_ready = bool(user and instagram_publish_ready(user))
        if not fb_ready:
            self.fields['publish_to_facebook'].disabled = True
            self.fields['publish_to_facebook'].help_text = (
                'Connect Facebook from Social Connections first.'
            )
        elif not instance:
            self.fields['publish_to_facebook'].initial = True
        if not ig_ready:
            self.fields['publish_to_instagram'].disabled = True
            self.fields['publish_to_instagram'].help_text = (
                'Connect Instagram from Social Connections first.'
            )
        elif not instance:
            self.fields['publish_to_instagram'].initial = True

        if instance and instance.status == Post.STATUS_DRAFT:
            if instance.scheduled_at:
                self.fields['publish_action'].initial = self.SCHEDULE
                local = timezone.localtime(instance.scheduled_at)
                self.fields['scheduled_at'].initial = local.strftime('%Y-%m-%dT%H:%M')
            else:
                self.fields['publish_action'].initial = self.PUBLISH_NOW

    def _has_media(self, cleaned):
        instance = self.instance if self.instance and self.instance.pk else None
        generated_path = (cleaned.get('generated_image_path') or '').strip()
        generated_paths = (cleaned.get('generated_image_paths') or '').strip()
        library_ids = (cleaned.get('library_asset_ids') or '').strip()
        carousel_files = self.files.getlist('carousel_files') if getattr(self, 'files', None) else []
        has_existing = bool(
            instance and (
                instance.image
                or instance.video
                or instance.media_items.exists()
            )
        )
        return bool(
            generated_path
            or generated_paths
            or library_ids
            or carousel_files
            or has_existing
        )

    def clean(self):
        cleaned = super().clean()
        if getattr(self, 'draft_mode', False):
            return self._clean_draft(cleaned)

        action = cleaned.get('publish_action')
        scheduled_at = cleaned.get('scheduled_at')

        if action == self.SCHEDULE:
            if not scheduled_at:
                self.add_error('scheduled_at', 'Pick a date and time to schedule this post.')
            else:
                if timezone.is_naive(scheduled_at):
                    scheduled_at = timezone.make_aware(
                        scheduled_at,
                        timezone.get_current_timezone(),
                    )
                    cleaned['scheduled_at'] = scheduled_at
                now = timezone.now()
                if scheduled_at <= now:
                    if now - scheduled_at <= SCHEDULE_GRACE:
                        # Time slipped past while the upload was still in flight
                        cleaned['scheduled_at'] = now + timedelta(seconds=15)
                    else:
                        self.add_error('scheduled_at', 'Schedule time must be in the future.')

        if not self._has_media(cleaned):
            self.add_error(
                None,
                'Add media first: upload from your device, generate with AI, or pick from saved files.',
            )

        from .meta import facebook_publish_ready, instagram_publish_ready

        if not (self.user and facebook_publish_ready(self.user)):
            cleaned['publish_to_facebook'] = False
        if not (self.user and instagram_publish_ready(self.user)):
            cleaned['publish_to_instagram'] = False

        return cleaned

    def _clean_draft(self, cleaned):
        instance = self.instance if self.instance and self.instance.pk else None
        description = (cleaned.get('description') or '').strip()
        caption = (cleaned.get('caption') or '').strip()
        image_prompt = (cleaned.get('image_prompt') or '').strip()
        has_media = self._has_media(cleaned)

        if not any([description, caption, image_prompt, has_media]):
            raise forms.ValidationError('Add some text or media before saving a draft.')

        scheduled_at = cleaned.get('scheduled_at')
        if scheduled_at and timezone.is_naive(scheduled_at):
            cleaned['scheduled_at'] = timezone.make_aware(
                scheduled_at,
                timezone.get_current_timezone(),
            )

        from .meta import facebook_publish_ready, instagram_publish_ready

        if not (self.user and facebook_publish_ready(self.user)):
            cleaned['publish_to_facebook'] = False
        if not (self.user and instagram_publish_ready(self.user)):
            cleaned['publish_to_instagram'] = False

        return cleaned

    def save_draft(self, post):
        post.status = Post.STATUS_DRAFT
        post.published_at = None
        action = self.cleaned_data.get('publish_action')
        if action == self.SCHEDULE and self.cleaned_data.get('scheduled_at'):
            post.scheduled_at = self.cleaned_data['scheduled_at']
        elif action != self.SCHEDULE:
            post.scheduled_at = None
        post.save()
        return post

    def apply_publish_action(self, post):
        action = self.cleaned_data['publish_action']
        if action == self.PUBLISH_NOW:
            post.mark_publishing()
        else:
            post.mark_scheduled(self.cleaned_data['scheduled_at'])
        return post
