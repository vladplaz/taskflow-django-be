from unittest.mock import patch

from django.conf import settings
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase

from taskflow.middleware import DevelopmentLatencyMiddleware


class TestSettingsTests(SimpleTestCase):
    def test_test_settings_use_the_in_memory_channel_layer(self):
        self.assertEqual(
            settings.CHANNEL_LAYERS["default"]["BACKEND"],
            "channels.layers.InMemoryChannelLayer",
        )

    def test_test_settings_collect_email_instead_of_sending_it(self):
        self.assertEqual(
            settings.EMAIL_BACKEND,
            "django.core.mail.backends.locmem.EmailBackend",
        )

    def test_test_settings_disable_throttling_by_default(self):
        rates = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
        self.assertTrue(all(rate is None for rate in rates.values()))


class DevelopmentLatencyMiddlewareTests(SimpleTestCase):
    def test_waits_for_a_random_duration_between_half_and_one_second(self):
        def get_response(request):
            return HttpResponse("ok")

        middleware = DevelopmentLatencyMiddleware(get_response)

        with (
            patch("taskflow.middleware.random.uniform", return_value=0.75) as random_uniform,
            patch("taskflow.middleware.time.sleep") as sleep,
        ):
            response = middleware(RequestFactory().get("/api/v1/auth/csrf/"))

        self.assertEqual(response.status_code, 200)
        random_uniform.assert_called_once_with(0.5, 1.0)
        sleep.assert_called_once_with(0.75)
