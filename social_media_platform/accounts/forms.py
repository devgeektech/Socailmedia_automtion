from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.models import User


class EmailLoginForm(AuthenticationForm):
    """Authenticate with email + password (username field stores email in the form)."""

    username = forms.EmailField(
        label='Email',
        widget=forms.EmailInput(attrs={
            'autocomplete': 'email',
            'autofocus': True,
            'class': 'glass-input',
            'placeholder': 'Email',
        }),
    )
    password = forms.CharField(
        label='Password',
        strip=False,
        widget=forms.PasswordInput(attrs={
            'autocomplete': 'current-password',
            'class': 'glass-input',
            'placeholder': 'Password',
        }),
    )

    error_messages = {
        **AuthenticationForm.error_messages,
        'invalid_login': 'Please enter a correct email and password.',
    }

    def clean(self):
        email = (self.cleaned_data.get('username') or '').strip().lower()
        password = self.cleaned_data.get('password')

        if email and password:
            matched = User.objects.filter(email__iexact=email).first()
            if matched is None:
                raise self.get_invalid_login_error()

            self.cleaned_data['username'] = matched.username
            self.user_cache = None
            return super().clean()

        return self.cleaned_data


class SignUpForm(UserCreationForm):
    email = forms.EmailField(
        required=True,
        help_text='Must be unique. You will sign in with this email.',
    )
    first_name = forms.CharField(max_length=50, required=True, label='First name')
    last_name = forms.CharField(max_length=50, required=True, label='Last name')

    class Meta:
        model = User
        fields = ('first_name', 'last_name', 'email', 'password1', 'password2')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # UserCreationForm may still expose username — keep it off the signup UI.
        self.fields.pop('username', None)

    def clean_email(self):
        email = (self.cleaned_data.get('email') or '').strip().lower()
        if not email:
            raise forms.ValidationError('Email is required.')
        if len(email) > 150:
            raise forms.ValidationError('Email is too long.')
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError('An account with this email already exists.')
        if User.objects.filter(username__iexact=email).exists():
            raise forms.ValidationError('An account with this email already exists.')
        return email

    def save(self, commit=True):
        # Bypass UserCreationForm.save username requirement by building the user here.
        user = User(
            username=self.cleaned_data['email'][:150],
            email=self.cleaned_data['email'],
            first_name=self.cleaned_data['first_name'].strip(),
            last_name=self.cleaned_data['last_name'].strip(),
        )
        user.set_password(self.cleaned_data['password1'])
        if commit:
            user.save()
        return user
