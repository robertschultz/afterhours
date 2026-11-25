# Sourced by project launchers after changing to the project directory.
if [ -n "${AFTERHOURS_PYTHON:-}" ]; then
  DJ_PYTHON="$AFTERHOURS_PYTHON"
elif [ -x .venv/bin/python ]; then
  DJ_PYTHON="$PWD/.venv/bin/python"
else
  DJ_PYTHON=python3
fi
