from unittest.mock import patch

from django.core.cache import cache
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient, APITestCase
from rest_framework.throttling import ScopedRateThrottle

from apps.accounts.models import User


def with_throttle_rates(**rates):
    """Temporarily enable throttle rates for one test.

    DRF binds THROTTLE_RATES at import time, so override_settings never
    reaches it.
    """
    return patch.dict(ScopedRateThrottle.THROTTLE_RATES, rates)


class AuthenticationApiTests(APITestCase):
    def setUp(self):
        self.client = APIClient(enforce_csrf_checks=True)

    def csrf_token(self):
        http_response = self.client.get(reverse("accounts:csrf"))
        self.assertEqual(http_response.status_code, status.HTTP_204_NO_CONTENT)
        return http_response.cookies["csrftoken"].value

    def post_with_csrf(self, url_name, payload=None):
        return self.client.post(
            reverse(url_name),
            payload or {},
            format="json",
            HTTP_X_CSRFTOKEN=self.csrf_token(),
        )

    def register(self, **overrides):
        payload = {
            "email": "maya@example.com",
            "display_name": "Maya",
            "password": "strong-password-123",
            **overrides,
        }
        return self.post_with_csrf("accounts:register", payload)

    def test_csrf_endpoint_sets_cookie_for_the_react_client(self):
        http_response = self.client.get(reverse("accounts:csrf"))

        self.assertEqual(http_response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertIn("csrftoken", http_response.cookies)

    def test_register_creates_an_email_based_user(self):
        http_response = self.register()

        self.assertEqual(http_response.status_code, status.HTTP_201_CREATED)
        self.assertIn("id", http_response.data)
        self.assertEqual(http_response.data["email"], "maya@example.com")
        self.assertTrue(User.objects.filter(email="maya@example.com").exists())

    def test_register_rejects_case_insensitive_duplicate_emails(self):
        self.register(email="Maya@example.com")

        response = self.register(email="maya@example.com")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["email"],
            ["User with this email already exists"],
        )

    def test_default_serializer_errors_are_capitalized_without_a_trailing_period(self):
        response = self.post_with_csrf(
            "accounts:register",
            {
                "email": "not-an-email",
                "display_name": "",
                "password": "strong-password-123",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        messages = [
            message for field_messages in response.data.values() for message in field_messages
        ]
        self.assertTrue(messages)
        self.assertTrue(all(message[0].isupper() for message in messages))
        self.assertTrue(all(not message.endswith(".") for message in messages))

    def test_register_rejects_a_password_that_matches_the_email(self):
        response = self.register(password="maya@example.com")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("password", response.data)

    def test_login_creates_a_server_side_session_and_me_uses_it(self):
        self.register()

        login_response = self.post_with_csrf(
            "accounts:login",
            {"email": "maya@example.com", "password": "strong-password-123"},
        )
        me_response = self.client.get(reverse("accounts:me"))

        self.assertEqual(login_response.status_code, status.HTTP_200_OK)
        self.assertEqual(login_response.data["display_name"], "Maya")
        self.assertIn("sessionid", login_response.cookies)
        self.assertTrue(login_response.cookies["sessionid"]["httponly"])
        self.assertEqual(me_response.status_code, status.HTTP_200_OK)
        self.assertEqual(me_response.data["email"], "maya@example.com")

    def test_logout_invalidates_the_session(self):
        self.register()
        self.post_with_csrf(
            "accounts:login",
            {"email": "maya@example.com", "password": "strong-password-123"},
        )

        logout_response = self.post_with_csrf("accounts:logout")
        me_response = self.client.get(reverse("accounts:me"))

        self.assertEqual(logout_response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(logout_response.cookies["sessionid"]["max-age"], 0)
        self.assertEqual(me_response.status_code, status.HTTP_403_FORBIDDEN)

    def test_login_rejects_invalid_credentials(self):
        self.register()

        http_response = self.post_with_csrf(
            "accounts:login",
            {"email": "maya@example.com", "password": "not-the-password"},
        )

        self.assertEqual(http_response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_model_has_no_unused_name_fields(self):
        user = User.objects.create_user(
            email="names@example.com",
            password="strong-password-123",
            display_name="Named Person",
        )

        field_names = {field.name for field in User._meta.get_fields()}
        self.assertNotIn("first_name", field_names)
        self.assertNotIn("last_name", field_names)
        self.assertNotIn("username", field_names)
        self.assertEqual(user.get_full_name(), "Named Person")
        self.assertEqual(user.get_short_name(), "Named Person")


class LoginThrottlingTests(APITestCase):
    """Password guessing is the main threat to a session-authenticated API."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        User.objects.create_user(
            email="maya@example.com",
            password="strong-password-123",
            display_name="Maya",
        )

    def attempt_login(self, password="wrong-password"):
        return self.client.post(
            reverse("accounts:login"),
            {"email": "maya@example.com", "password": password},
            format="json",
        )

    @with_throttle_rates(login="3/min")
    def test_repeated_failed_logins_are_throttled(self):
        statuses = [self.attempt_login().status_code for _ in range(5)]

        self.assertEqual(statuses[:3], [status.HTTP_400_BAD_REQUEST] * 3)
        self.assertIn(status.HTTP_429_TOO_MANY_REQUESTS, statuses)

    def test_throttling_is_disabled_in_the_test_settings_by_default(self):
        statuses = [self.attempt_login().status_code for _ in range(8)]

        self.assertNotIn(status.HTTP_429_TOO_MANY_REQUESTS, statuses)
