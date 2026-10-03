from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

# Single source: pyproject.toml's [project] version (bumped by
# scripts/release.sh, see README "Releasing"), read from the installed
# package's metadata. `pip install -e .` (the Dockerfile does it) provides it.
try:
    __version__ = version("ovc-backend")
except PackageNotFoundError:  # source tree that was never pip-installed
    __version__ = "0.0.0+unknown"
