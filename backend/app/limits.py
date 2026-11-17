"""Process-startup resource limits for bounded API request bodies."""

import os


DEFAULT_BODY_LIMIT_BYTES = 2 * 1024 * 1024
MAX_EXPORT_BODY_BYTES = DEFAULT_BODY_LIMIT_BYTES


def _positive_environment_limit(name: str) -> int:
    value = os.environ.get(name)
    if value is None:
        return DEFAULT_BODY_LIMIT_BYTES
    try:
        if not value.isascii() or not value.isdecimal():
            raise ValueError
        parsed = int(value, 10)
    except ValueError:
        raise RuntimeError(f"{name} must be a positive integer.") from None
    if parsed < 1:
        raise RuntimeError(f"{name} must be a positive integer.")
    return parsed


MAX_UPLOAD_BYTES = _positive_environment_limit("KHDV_MAX_UPLOAD_BYTES")
MAX_SCHEDULE_BODY_BYTES = _positive_environment_limit(
    "KHDV_MAX_SCHEDULE_BODY_BYTES"
)
