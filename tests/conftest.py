"""Pytest configuration allowing standalone tests of SG Fleet modules."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# If homeassistant is not installed in the local test environment, register a
# namespace package stub for custom_components.sgfleet so api.py and const.py
# can be imported directly without triggering __init__.py's HA imports.
if importlib.util.find_spec("homeassistant") is None:
    cc_pkg = types.ModuleType("custom_components")
    cc_pkg.__path__ = [str(ROOT / "custom_components")]
    sys.modules.setdefault("custom_components", cc_pkg)

    sg_pkg = types.ModuleType("custom_components.sgfleet")
    sg_pkg.__path__ = [str(ROOT / "custom_components" / "sgfleet")]
    cc_pkg.sgfleet = sg_pkg  # type: ignore[attr-defined]
    sys.modules.setdefault("custom_components.sgfleet", sg_pkg)
