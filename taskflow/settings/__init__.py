"""Settings package.

Choose a module explicitly through ``DJANGO_SETTINGS_MODULE``:

* ``taskflow.settings.dev``  - local development (manage.py default)
* ``taskflow.settings.test`` - automated tests (``manage.py test`` default)
* ``taskflow.settings.prod`` - deployment

There is intentionally no importable configuration in this file, so importing
``taskflow.settings`` by mistake fails loudly instead of loading a half-defined
environment.
"""
