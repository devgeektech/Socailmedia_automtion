from django import forms
from django.utils import timezone

from .models import Post


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
    # Relative media path from AJAX generate step (e.g. posts/ai_temp/...)
    generated_image_path = forms.CharField(
        required=False,
        widget=forms.HiddenInput(attrs={'id': 'id_generated_image_path'}),
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

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        instance = kwargs.get('instance')
        self.fields['image_prompt'].required = not (instance and instance.pk and instance.image)

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
            # New posts: Facebook selected by default when connected
            self.fields['publish_to_facebook'].initial = True
        if not ig_ready:
            self.fields['publish_to_instagram'].disabled = True
            self.fields['publish_to_instagram'].help_text = (
                'Connect Instagram from Social Connections first.'
            )
        elif not instance:
            self.fields['publish_to_instagram'].initial = True

    def clean(self):
        cleaned = super().clean()
        action = cleaned.get('publish_action')
        scheduled_at = cleaned.get('scheduled_at')
        image_prompt = (cleaned.get('image_prompt') or '').strip()
        generated_path = (cleaned.get('generated_image_path') or '').strip()
        instance = self.instance if self.instance and self.instance.pk else None

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
                if scheduled_at <= timezone.now():
                    self.add_error('scheduled_at', 'Schedule time must be in the future.')

        needs_image = not instance or not instance.image or cleaned.get('regenerate_image')
        if needs_image and not image_prompt and not generated_path:
            self.add_error(
                'image_prompt',
                'Enter a prompt and generate an AI image for this post.',
            )

        # Disabled checkboxes are omitted from POST — keep False when not ready
        from .meta import facebook_publish_ready, instagram_publish_ready

        if not (self.user and facebook_publish_ready(self.user)):
            cleaned['publish_to_facebook'] = False
        if not (self.user and instagram_publish_ready(self.user)):
            cleaned['publish_to_instagram'] = False

        return cleaned

    def apply_publish_action(self, post):
        """
        Publish now → send to selected platforms (Facebook/Instagram) immediately.
        Schedule → save for the background publisher at scheduled_at (no manual click).
        """
        from .meta import MetaAPIError
        from .publisher import publish_post

        action = self.cleaned_data['publish_action']
        if action == self.PUBLISH_NOW:
            try:
                publish_post(post)
                post.refresh_from_db()
            except MetaAPIError:
                post.status = Post.STATUS_FAILED
                post.save(update_fields=['status', 'updated_at'])
                raise
        else:
            post.mark_scheduled(self.cleaned_data['scheduled_at'])
        return post
