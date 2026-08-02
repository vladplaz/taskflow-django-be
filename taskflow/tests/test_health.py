from django.test import TestCase
from django.urls import reverse


class HealthEndpointTests(TestCase):
    def test_reports_ok_without_authentication(self):
        response = self.client.get(reverse("health"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_survives_an_unreachable_database(self):
        """A failing probe restarts the container, so it must not depend on
        a service that can blip."""
        with self.assertNumQueries(0):
            self.client.get(reverse("health"))
