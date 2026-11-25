"""Local operator defaults bounded by engine safety and approval limits."""

import os


def bounded_setting(name, default, lower, upper):
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if lower <= value <= upper else default


def track_seconds():
    return bounded_setting('AFTERHOURS_TRACK_SECONDS', 30, 15, 300)


def curation_seconds():
    return 60 * bounded_setting('AFTERHOURS_CURATION_MINUTES', 30, 1, 360)
