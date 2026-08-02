from rest_framework.views import exception_handler


def normalize_error_message(value: str) -> str:
    """Return a consistently styled API error without changing its meaning."""
    message = value.strip().rstrip(".")
    if not message:
        return message
    return f"{message[0].upper()}{message[1:]}"


def normalize_error_data(value):
    if isinstance(value, dict):
        return {key: normalize_error_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize_error_data(item) for item in value]
    if isinstance(value, str):
        return normalize_error_message(value)
    return value


def normalized_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is not None:
        response.data = normalize_error_data(response.data)
    return response
