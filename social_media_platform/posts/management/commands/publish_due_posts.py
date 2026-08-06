from django.core.management.base import BaseCommand

from posts.publisher import publish_due_posts


class Command(BaseCommand):
    help = 'Publish posts whose scheduled_at time has passed.'

    def handle(self, *args, **options):
        count = publish_due_posts()
        self.stdout.write(self.style.SUCCESS(f'Published {count} scheduled post(s).'))
