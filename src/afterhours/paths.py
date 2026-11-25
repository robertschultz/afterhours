"""Locations for packaged assets and local Afterhours state."""

import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
SHARED_WEB = PACKAGE_ROOT / "web"


def data_root():
    configured = os.environ.get("AFTERHOURS_DATA_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    xdg_data = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share"
    return (Path(xdg_data).expanduser() / "afterhours").resolve()


def analysis_reports():
    configured = os.environ.get("AFTERHOURS_ANALYSIS_REPORTS_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return data_root() / ".runtime/audio-analysis"
