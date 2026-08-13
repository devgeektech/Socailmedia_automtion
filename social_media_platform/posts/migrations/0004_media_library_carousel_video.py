# Generated manually for MediaAsset, PostMedia, video, media_type

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('posts', '0003_social_publish_ids'),
    ]

    operations = [
        migrations.CreateModel(
            name='MediaAsset',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('file', models.FileField(upload_to='library/%Y/%m/')),
                ('kind', models.CharField(choices=[('image', 'Image'), ('video', 'Video')], default='image', max_length=16)),
                ('source', models.CharField(choices=[('upload', 'Upload'), ('ai', 'AI generated')], default='upload', max_length=16)),
                ('prompt', models.TextField(blank=True)),
                ('original_name', models.CharField(blank=True, max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='media_assets', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddField(
            model_name='post',
            name='media_type',
            field=models.CharField(choices=[('image', 'Single image'), ('carousel', 'Carousel'), ('video', 'Video')], default='image', max_length=16),
        ),
        migrations.AddField(
            model_name='post',
            name='video',
            field=models.FileField(blank=True, null=True, upload_to='posts/videos/'),
        ),
        migrations.CreateModel(
            name='PostMedia',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('image', models.ImageField(blank=True, null=True, upload_to='posts/carousel/')),
                ('order', models.PositiveIntegerField(default=0)),
                ('asset', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='post_links', to='posts.mediaasset')),
                ('post', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='media_items', to='posts.post')),
            ],
            options={
                'ordering': ['order', 'id'],
            },
        ),
    ]
