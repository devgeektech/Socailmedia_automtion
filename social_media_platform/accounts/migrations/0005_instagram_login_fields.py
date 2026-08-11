from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0004_instagram_user_token_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='userprofile',
            name='instagram_user_id',
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='instagram_access_token',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='instagram_token_expires_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='instagram_connected_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
