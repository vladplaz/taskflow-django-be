import os

from django.core.wsgi import get_wsgi_application


# Not dev: a deployment that forgets the variable must fail, not boot insecure.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "taskflow.settings.prod")

application = get_wsgi_application()
