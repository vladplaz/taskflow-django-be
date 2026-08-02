import random
import time


class DevelopmentLatencyMiddleware:
    """Make local API interactions feel closer to a real network connection."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        time.sleep(random.uniform(0.5, 1.0))
        return self.get_response(request)
