from django.apps import AppConfig


class PostsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'posts'

    def ready(self):
        # Start background publisher when the app loads (runserver / production).
        from .scheduler import start_publish_scheduler

        start_publish_scheduler()
