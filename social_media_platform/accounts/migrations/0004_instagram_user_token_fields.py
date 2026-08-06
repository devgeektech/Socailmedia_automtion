from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0003_meta_social_connection'),
    ]

    operations = [
        migrations.AddField(
            model_name='userprofile',
            name='facebook_user_access_token',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='instagram_username',
            field=models.CharField(max_length=255, blank=True),
        ),
    ]
