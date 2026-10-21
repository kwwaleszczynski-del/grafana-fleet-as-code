"""NREL PVDAQ system 34 (OEDI, doi:10.25984/1846021, CC BY 4.0): one CSV per day, 15-minute values.

Units in the source: power channels in hectowatts (0.1 kW, checked against DC voltage x current), temperatures
in degF (0 and -40 degF are sensor dropouts, the ambient sensor also has single-step dips that are removed
with a rolling-median filter), local standard time without daylight saving. On some days the
inverter channels are missing entirely (e.g. while the inverter was offline); they become NaN.

Usage:
    from fleet_as_code.sources import pvdaq
    pv, events = pvdaq.load(Path("data/pvdaq/system_34"), "2016-05-25", "2016-06-25")
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from fleet_as_code import config

log = logging.getLogger(__name__)

STEP = pd.Timedelta("15min")
COLUMNS = {"ac_power_hw__2695": "ac_kw", "dc_power_hw__2694": "dc_kw", "poa_irradiance__2679": "poa_wm2",
           "module_temp_f__2689": "module_temp_c", "ambient_temp_f__2688": "ambient_temp_c",
           "inverter_error_code__2690": "error_code"}


def _celsius(f: pd.Series) -> pd.Series:
    return ((f - 32) * 5 / 9).where((f != 0) & (f > -40))


def despike(x: pd.Series, window: int = 5, max_dev: float = 10.0) -> pd.Series:
    """Drop single-step sensor glitches: values more than max_dev from the centred rolling median."""
    med = x.rolling(window, center=True, min_periods=3).median()
    return x.where((x - med).abs() <= max_dev)


def load(day_dir: Path, start: str, end: str, plant: config.PvPlant = config.PV) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Daily files for local dates in [start, end) -> 15-minute frame (UTC `time`) and inverter fault events."""
    days = pd.date_range(start, end, freq="D", inclusive="left")
    files = [Path(day_dir) / f"system_34__date_{d:%Y_%m_%d}.csv" for d in days]
    missing = [f.name for f in files if not f.exists()]
    if missing:
        raise FileNotFoundError(f"{len(missing)} PVDAQ day files missing in {day_dir}, e.g. {missing[0]}")
    raw = pd.concat([pd.read_csv(f).reindex(columns=["measured_on", *COLUMNS]) for f in files], ignore_index=True)
    raw = raw.rename(columns=COLUMNS)
    time = pd.to_datetime(raw["measured_on"]).dt.round(STEP).dt.tz_localize(plant.tz).dt.tz_convert("UTC")
    df = raw.drop(columns="measured_on").assign(time=time).drop_duplicates("time").reset_index(drop=True)
    df["ac_kw"] = (df["ac_kw"] * 0.1).clip(lower=0)        # night-time tare (-0.2 kW) to zero
    df["dc_kw"] = (df["dc_kw"] * 0.1).clip(lower=0)
    df["poa_wm2"] = df["poa_wm2"].clip(lower=0)
    df["module_temp_c"] = _celsius(df["module_temp_c"])
    df["ambient_temp_c"] = despike(_celsius(df["ambient_temp_c"]))
    df["energy_kwh"] = df["ac_kw"] * STEP.total_seconds() / 3600
    df = add_state(df)
    log.info("PVDAQ: %d rows %s .. %s, %.1f %% without AC power", len(df), df["time"].min(), df["time"].max(),
             100 * df["ac_kw"].isna().mean())
    return df, fault_events(df)


def add_state(df: pd.DataFrame) -> pd.DataFrame:
    """state: 4 when the inverter reports an error code, else 0 producing / 1 not producing.
    available (IEC 61724-3 style, daylight only): 1 when producing while POA > PV_DAYLIGHT_WM2, NaN at night."""
    code = df["error_code"]
    producing = df["ac_kw"] > config.PV_PRODUCING_KW
    state = np.select([code.fillna(0) != 0, producing], [4, 0], default=1)
    daylight = df["poa_wm2"] > config.PV_DAYLIGHT_WM2
    available = producing.astype(float).where(daylight & df["ac_kw"].notna())
    return df.assign(state=np.where(df["ac_kw"].isna() & code.isna(), np.nan, state), available=available)


def fault_events(df: pd.DataFrame) -> pd.DataFrame:
    """One event each time the inverter error code changes to a non-zero value."""
    code = df["error_code"].ffill()
    new = (code != code.shift()) & (code.fillna(0) != 0)
    ev = df.loc[new, ["time", "error_code"]]
    return pd.DataFrame({"start": ev["time"], "asset": config.PV.asset, "status": "Stop",
                         "code": ev["error_code"].astype(int), "message": [f"Inverter error code {c:.0f}" for c in ev["error_code"]],
                         "category": "Forced outage"}).reset_index(drop=True)
