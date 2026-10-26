"""Fixtures for the crew_hub Home Assistant tests. Run from the repo root:
/home/node/.local/opt/ha-test-venv/bin/python -m pytest tests/ha
"""

import copy
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # makes `custom_components.crew_hub` importable for HA's loader

FIXTURE = ROOT / "card" / "fixtures" / "snapshot-sample.json"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def nas_snapshot():
    """The card fixture's nas machine: Jamfin working, Mr Manager + Music Assistant waiting, error demo + Crew Hub idle."""
    data = json.loads(FIXTURE.read_text())
    snap = copy.deepcopy(data["nas"])
    snap.pop("status", None)
    return snap


@pytest.fixture
def expected_lingering_timers() -> bool:
    """The MQTT integration's own misc periodic timer outlives each test; crew_hub schedules no timers."""
    return True
