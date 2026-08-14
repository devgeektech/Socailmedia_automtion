from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('posts', '0004_media_library_carousel_video'),
    ]

    operations = [
        migrations.AlterField(
            model_name='post',
            name='status',
            field=models.CharField(
                choices=[
                    ('draft', 'Draft'),
                    ('publishing', 'Publishing'),
                    ('scheduled', 'Scheduled'),
                    ('published', 'Published'),
                    ('failed', 'Failed'),
                ],
                default='draft',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='post',
            name='cancel_requested',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='post',
            name='publish_started_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
