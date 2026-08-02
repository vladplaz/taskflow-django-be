"""The environment readers, and the `.env` loading they depend on."""

import os
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from taskflow.settings import env


class DotenvLoadingTests(SimpleTestCase):
    def test_the_project_root_is_where_env_and_compose_live(self):
        self.assertTrue((env.PROJECT_ROOT / "compose.yaml").exists())
        self.assertTrue((env.PROJECT_ROOT / ".env.example").exists())
        self.assertEqual(env.PROJECT_ROOT, Path(__file__).resolve().parent.parent.parent)

    def test_a_real_environment_variable_beats_the_file(self):
        """`override=False` is what keeps CI and containers in charge."""
        from dotenv import load_dotenv

        with patch.dict(os.environ, {"TASKFLOW_PROBE": "from-environment"}):
            load_dotenv(env.PROJECT_ROOT / ".env", override=False)
            self.assertEqual(os.environ["TASKFLOW_PROBE"], "from-environment")


class EnvReaderTests(SimpleTestCase):
    def test_env_bool_accepts_the_documented_spellings(self):
        for raw in ("1", "true", "TRUE", "yes", "on"):
            with patch.dict(os.environ, {"TASKFLOW_FLAG": raw}):
                self.assertIs(env.env_bool("TASKFLOW_FLAG", False), True)
        for raw in ("0", "false", "no", "off"):
            with patch.dict(os.environ, {"TASKFLOW_FLAG": raw}):
                self.assertIs(env.env_bool("TASKFLOW_FLAG", True), False)

    def test_env_bool_refuses_a_value_it_cannot_read(self):
        with patch.dict(os.environ, {"TASKFLOW_FLAG": "maybe"}):
            with self.assertRaises(ImproperlyConfigured):
                env.env_bool("TASKFLOW_FLAG", False)

    def test_env_int_refuses_a_non_integer(self):
        with patch.dict(os.environ, {"TASKFLOW_NUMBER": "twelve"}):
            with self.assertRaises(ImproperlyConfigured):
                env.env_int("TASKFLOW_NUMBER", 1)

    def test_env_list_splits_and_strips(self):
        with patch.dict(os.environ, {"TASKFLOW_HOSTS": " a.example.com , b.example.com ,"}):
            self.assertEqual(
                env.env_list("TASKFLOW_HOSTS", []),
                ["a.example.com", "b.example.com"],
            )

    def test_env_str_required_refuses_to_boot_without_a_value(self):
        """Production reads its secrets this way, so a gap fails at startup."""
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ImproperlyConfigured):
                env.env_str_required("DJANGO_SECRET_KEY")


class RedisUrlTests(SimpleTestCase):
    def resolve(self, **environ):
        with patch.dict(os.environ, environ, clear=True):
            return env.env_redis_url()

    def test_a_url_is_passed_through_untouched(self):
        self.assertEqual(
            self.resolve(REDIS_URL="rediss://default:tok@example.upstash.io:6379/0"),
            "rediss://default:tok@example.upstash.io:6379/0",
        )

    def test_a_rest_endpoint_is_refused_with_a_usable_message(self):
        """The REST product cannot carry a channel layer; failing later is worse."""
        with self.assertRaises(ImproperlyConfigured) as caught:
            self.resolve(REDIS_URL="https://example.upstash.io")
        self.assertIn("rediss://", str(caught.exception))

    def test_a_url_in_no_known_scheme_is_refused(self):
        with self.assertRaises(ImproperlyConfigured):
            self.resolve(REDIS_URL="example.upstash.io:6379")

    def test_host_and_token_compose_into_a_tls_url(self):
        self.assertEqual(
            self.resolve(REDIS_HOST="example.upstash.io", REDIS_PASSWORD="tok"),
            "rediss://default:tok@example.upstash.io:6379/0",
        )

    def test_a_token_with_url_syntax_in_it_is_quoted(self):
        self.assertEqual(
            self.resolve(REDIS_HOST="example.upstash.io", REDIS_PASSWORD="a/b@c#d"),
            "rediss://default:a%2Fb%40c%23d@example.upstash.io:6379/0",
        )

    def test_a_hostname_pasted_as_a_url_is_reduced_to_its_host(self):
        self.assertEqual(
            self.resolve(REDIS_HOST="https://example.upstash.io/", REDIS_PASSWORD="tok"),
            "rediss://default:tok@example.upstash.io:6379/0",
        )

    def test_a_local_host_gets_no_tls_and_no_credentials(self):
        self.assertEqual(self.resolve(REDIS_HOST="localhost"), "redis://localhost:6379/0")

    def test_tls_can_be_forced_off_for_a_remote_host(self):
        self.assertEqual(
            self.resolve(REDIS_HOST="cache.internal", REDIS_PASSWORD="tok", REDIS_TLS="false"),
            "redis://default:tok@cache.internal:6379/0",
        )

    def test_nothing_configured_falls_back_to_the_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                env.env_redis_url(default="redis://localhost:6380/0"), "redis://localhost:6380/0"
            )

    def test_nothing_configured_and_no_default_refuses_to_boot(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ImproperlyConfigured):
                env.env_redis_url()


class EntryPointDefaultTests(SimpleTestCase):
    """A deployment that forgets DJANGO_SETTINGS_MODULE must fail, not boot dev."""

    def test_the_server_entry_points_default_to_production_settings(self):
        for module in ("wsgi", "asgi"):
            source = (env.PROJECT_ROOT / "taskflow" / f"{module}.py").read_text()
            self.assertIn('"DJANGO_SETTINGS_MODULE", "taskflow.settings.prod"', source)


class ThrottleTests(SimpleTestCase):
    def test_every_endpoint_has_a_ceiling_not_only_the_scoped_ones(self):
        from django.conf import settings

        classes = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_CLASSES"]
        self.assertIn("rest_framework.throttling.AnonRateThrottle", classes)
        self.assertIn("rest_framework.throttling.UserRateThrottle", classes)
        self.assertIn("rest_framework.throttling.ScopedRateThrottle", classes)


class TestSettingsTests(SimpleTestCase):
    def test_throttling_is_off_for_every_scope(self):
        """Including the global ones, or unrelated tests start rate limiting."""
        from django.conf import settings

        rates = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
        self.assertEqual(set(rates.values()), {None})
        self.assertIn("anon", rates)
        self.assertIn("user", rates)

    def test_the_latency_middleware_is_never_active_under_test(self):
        """dev.py adds it from the environment; the suite must not inherit it."""
        from django.conf import settings

        self.assertFalse(
            any("DevelopmentLatencyMiddleware" in entry for entry in settings.MIDDLEWARE)
        )
