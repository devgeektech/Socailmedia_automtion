from django.core.management.base import BaseCommand

from subscriptions.models import Plan


class Command(BaseCommand):
    help = 'Seed default subscription plans'

    def handle(self, *args, **options):
        plans = [
            {
                'name': 'Free Trial',
                'slug': 'free-trial',
                'plan_type': 'free_trial',
                'price': 0,
                'duration_days': 14,
                'is_popular': False,
                'features': [
                    '14-day free trial',
                    '5 scheduled posts',
                    'Facebook & Instagram',
                    'Basic analytics',
                ],
            },
            {
                'name': 'Monthly Starter',
                'slug': 'monthly-starter',
                'plan_type': 'monthly',
                'price': 9.99,
                'duration_days': 30,
                'is_popular': False,
                'features': [
                    '50 posts per month',
                    'Facebook & Instagram',
                    'Post scheduling',
                    'Email support',
                ],
            },
            {
                'name': 'Monthly Pro',
                'slug': 'monthly-pro',
                'plan_type': 'monthly',
                'price': 19.99,
                'duration_days': 30,
                'is_popular': True,
                'features': [
                    'Unlimited posts',
                    'Facebook & Instagram',
                    'Post scheduling',
                    'Analytics dashboard',
                    'Priority support',
                ],
            },
        ]

        for plan_data in plans:
            Plan.objects.update_or_create(slug=plan_data['slug'], defaults=plan_data)

        self.stdout.write(self.style.SUCCESS(f'Seeded {len(plans)} plans successfully.'))
