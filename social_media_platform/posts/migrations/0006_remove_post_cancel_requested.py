from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('posts', '0005_post_publish_cancellation'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='post',
            name='cancel_requested',
        ),
    ]
