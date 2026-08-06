from django.db import migrations


def dedupe_user_emails(apps, schema_editor):
    """Make existing emails unique so the unique index can be created."""
    User = apps.get_model('auth', 'User')
    seen = {}
    for user in User.objects.exclude(email='').order_by('id'):
        email = (user.email or '').strip().lower()
        if not email:
            continue
        if email in seen:
            local, _, domain = email.partition('@')
            user.email = f'{local}+dup{user.id}@{domain}' if domain else f'{email}+dup{user.id}'
            user.save(update_fields=['email'])
        else:
            if user.email != email:
                user.email = email
                user.save(update_fields=['email'])
            seen[email] = user.id


def noop_reverse(apps, schema_editor):
    pass


def create_unique_email_index(apps, schema_editor):
    """Case-insensitive unique email index (Postgres + SQLite)."""
    vendor = schema_editor.connection.vendor
    if vendor == 'postgresql':
        schema_editor.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS auth_user_email_unique_ci '
            'ON auth_user (LOWER(email)) '
            "WHERE email IS NOT NULL AND email <> '';"
        )
    else:
        schema_editor.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS auth_user_email_unique_ci '
            'ON auth_user (email COLLATE NOCASE) '
            "WHERE email IS NOT NULL AND email != '';"
        )


def drop_unique_email_index(apps, schema_editor):
    schema_editor.execute('DROP INDEX IF EXISTS auth_user_email_unique_ci;')


class Migration(migrations.Migration):
    """Enforce unique non-empty emails on auth_user (case-insensitive)."""

    dependencies = [
        ('accounts', '0001_initial'),
        ('auth', '0012_alter_user_first_name_max_length'),
    ]

    operations = [
        migrations.RunPython(dedupe_user_emails, noop_reverse),
        migrations.RunPython(create_unique_email_index, drop_unique_email_index),
    ]
