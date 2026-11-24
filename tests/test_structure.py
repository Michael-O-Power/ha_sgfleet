"""Unit tests for SG Fleet sensor helpers, JSON translations, and manifest validity."""

from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path
import sys
import types

# Lightweight stubs for Home Assistant modules so pure helper logic & schemas
# can be tested in environments without full homeassistant installed.
ROOT = Path(__file__).resolve().parents[1]
COMPONENT_DIR = ROOT / "custom_components" / "sgfleet"


def test_manifest_and_hacs_json() -> None:
    manifest = json.loads((COMPONENT_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["domain"] == "sgfleet"
    assert manifest["config_flow"] is True
    assert manifest["iot_class"] == "cloud_polling"
    assert "version" in manifest

    hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
    assert hacs["name"]
    assert "AU" in hacs["country"]


def test_strings_and_translations_match() -> None:
    strings = json.loads((COMPONENT_DIR / "strings.json").read_text(encoding="utf-8"))
    en = json.loads((COMPONENT_DIR / "translations" / "en.json").read_text(encoding="utf-8"))
    assert strings == en
    assert "user" in strings["config"]["step"]
    assert "credentials" in strings["config"]["step"]
    assert "browser_pkce" in strings["config"]["step"]
    assert "reauth_confirm" in strings["config"]["step"]
    assert "captcha_required" in strings["config"]["error"]
    assert "mfa_required" in strings["config"]["error"]
    assert "init" in strings["options"]["step"]


def test_all_python_files_compile_cleanly() -> None:
    for py_file in sorted(COMPONENT_DIR.glob("*.py")):
        source = py_file.read_text(encoding="utf-8")
        compile(source, str(py_file), "exec")


def test_services_yaml_parses_cleanly() -> None:
    yaml_mod = None
    try:
        import yaml as yaml_mod  # type: ignore[no-redef]
    except ImportError:
        return
    services = yaml_mod.safe_load((COMPONENT_DIR / "services.yaml").read_text(encoding="utf-8"))
    assert set(services.keys()) == {
        "submit_home_charging_claim",
        "submit_reimbursement_claim",
        "register_odometer_reading",
        "refresh_data",
    }
    assert "confirm_ato_declaration" in services["submit_home_charging_claim"]["fields"]
    assert "dry_run" in services["submit_home_charging_claim"]["fields"]
