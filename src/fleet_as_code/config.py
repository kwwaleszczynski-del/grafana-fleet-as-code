"""Shared constants: assets, InfluxDB schema, state codes and the replay time scale.

Everything the dashboards, alert rules and the replay must agree on lives here, so a renamed field or a new
turbine is a one-line change followed by `python -m fleet_as_code generate`.

Usage:
    from fleet_as_code import config
    config.TURBINES[0].rated_kw          # 2050.0
    config.plant_to_wall(3600)           # seconds of wall time for 1 h of plant time at config.SPEED
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GRAFANA_DIR = ROOT / "grafana"
REFERENCE_CURVE = ROOT / "reference" / "kelmarsh_power_curves.csv"
SAMPLE_DIR = ROOT / "tests" / "data"

GRAFANA_VERSION = "13.2.4"      # also pinned in docker-compose.yml and portable.py (checked by the tests)
INFLUX_VERSION = "2.9.1"

INFLUX_ORG = "fleet-demo"
BUCKET = "fleet"                 # live replay data
REFERENCE_BUCKET = "reference"   # static data: the reference power curve
DATASOURCE_UID = "influxdb-fleet"
FOLDER_UID = "fleet"
FOLDER_TITLE = "Fleet"
# Alert rule files can only name a folder by title; a separate title avoids a duplicate "Fleet" folder when
# alerting is provisioned before the dashboards.
ALERT_FOLDER_TITLE = "Fleet alerts"

# Replay time scale: one second of wall time carries SPEED seconds of plant time. Alert windows are defined in
# plant time below and divided by SPEED when the provisioning files are generated, so a rule that looks at
# "the last 2 h" of turbine data evaluates `-2m` at 60x. Regenerate with `generate --speed 1` for real time.
SPEED = 60


@dataclass(frozen=True)
class Turbine:
    asset: str
    rated_kw: float
    hub_height_m: float


@dataclass(frozen=True)
class PvPlant:
    asset: str
    pdc_kwp: float
    pac_kw: float
    tz: str


WIND_SITE = "kelmarsh"
WIND_MODEL = "MM92"
# Kelmarsh wind farm (UK): 6 x Senvion MM92, 2.05 MW, from the dataset's static turbine file
TURBINES = tuple(Turbine(f"KWF{i}", 2050.0, 68.5 if i in (3, 6) else 78.5) for i in range(1, 7))

PV_SITE = "pvdaq-34"
# NREL PVDAQ system 34 (Las Vegas, NV): 611 x 240 W mono-Si on a flat roof, one 135 kW central inverter.
# Timestamps in the source files are local standard time without daylight saving.
PV = PvPlant("INV1", pdc_kwp=146.64, pac_kw=135.0, tz="Etc/GMT+8")

# InfluxDB schema. Tags: site, asset (plus severity/category on events). Fields per measurement:
MEASUREMENTS = {
    "wind_turbine": ("power_kw", "expected_kw", "wind_speed", "rotor_rpm", "pitch_deg", "ambient_temp_c",
                     "energy_kwh", "available", "state"),
    "pv_inverter": ("ac_kw", "dc_kw", "poa_wm2", "module_temp_c", "ambient_temp_c", "energy_kwh", "available",
                    "state", "error_code"),
    "event": ("code", "message"),
    "replay": ("source_time_ms", "plant_clock", "speed"),
    "power_curve": ("wind_speed", "power_kw", "samples"),   # in REFERENCE_BUCKET
}

# One state code for both asset types; the dashboards map them to text and colours.
STATES = {
    0: ("Producing", "green"),
    1: ("Ready, not producing", "blue"),
    2: ("Stopped, out of spec", "yellow"),
    3: ("Maintenance", "purple"),
    4: ("Fault", "red"),
}

# IEC 61400-26-1 style: these stop categories make a turbine unavailable, all others leave it available
UNAVAILABLE_CATEGORIES = {"Forced outage", "Scheduled Maintenance", "Planned corrective action", "Suspended"}
MAINTENANCE_CATEGORIES = {"Scheduled Maintenance", "Planned corrective action"}

PRODUCING_KW = 20.0          # wind turbine counts as producing above this 10-min mean power
PV_PRODUCING_KW = 1.0
PV_DAYLIGHT_WM2 = 50.0       # PV availability is only defined above this plane-of-array irradiance


def plant_to_wall(seconds: float, speed: float = SPEED, floor: float = 10.0) -> int:
    """Plant-time duration -> whole seconds of wall time, never below Grafana's 10 s evaluation interval."""
    return int(max(floor, round(seconds / speed)))
