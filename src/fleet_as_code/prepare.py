"""Turn the raw public downloads into compact replay files.

Outputs (in out_dir): wind.parquet, wind_events.parquet, pv.parquet, pv_events.parquet, plus the reference power
curves in reference/kelmarsh_power_curves.csv (one per turbine, built from a separate reference year so the
replay window is not used to judge itself).

Usage:
    from fleet_as_code.prepare import prepare
    prepare(Path("data/kelmarsh"), Path("data/pvdaq/system_34"), Path("data/replay"))
"""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from fleet_as_code import config
from fleet_as_code.reference import curves_by_asset, expected_by_asset
from fleet_as_code.sources import kelmarsh, pvdaq

log = logging.getLogger(__name__)

WIND_COLUMNS = ["time", "asset", "wind_speed", "power_kw", "expected_kw", "rotor_rpm", "pitch_deg",
                "ambient_temp_c", "energy_kwh", "available", "state"]
EVENT_COLUMNS = ["start", "asset", "status", "code", "message", "category"]


def build_reference(kelmarsh_dir: Path, year: int, out: Path = config.REFERENCE_CURVE) -> pd.DataFrame:
    scada, events = kelmarsh.load(kelmarsh.find_zips(kelmarsh_dir, [year]), f"{year}-01-01", f"{year + 1}-01-01")
    curves = curves_by_asset(kelmarsh.add_state(scada, events))
    out.parent.mkdir(parents=True, exist_ok=True)
    curves.round({"wind_speed": 3, "power_kw": 1}).to_csv(out, index=False)
    log.info("reference curves from %d: %d turbines, %d bins -> %s", year, curves["asset"].nunique(), len(curves), out)
    return curves


def prepare(kelmarsh_dir: Path, pvdaq_dir: Path, out_dir: Path, wind_window=("2020-10-15", "2020-11-15"),
            pv_window=("2016-05-25", "2016-06-25"), reference_year: int = 2019) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    curves = build_reference(kelmarsh_dir, reference_year)

    years = sorted({pd.Timestamp(wind_window[0]).year, (pd.Timestamp(wind_window[1]) - pd.Timedelta("1s")).year})
    scada, events = kelmarsh.load(kelmarsh.find_zips(kelmarsh_dir, years), *wind_window)
    scada = kelmarsh.add_state(scada, events)
    scada["expected_kw"] = expected_by_asset(scada, curves)
    scada[WIND_COLUMNS].to_parquet(out_dir / "wind.parquet", index=False)
    events[EVENT_COLUMNS].to_parquet(out_dir / "wind_events.parquet", index=False)

    pv, pv_events = pvdaq.load(pvdaq_dir, *pv_window)
    pv.to_parquet(out_dir / "pv.parquet", index=False)
    pv_events[EVENT_COLUMNS].to_parquet(out_dir / "pv_events.parquet", index=False)
    log.info("replay data in %s: wind %d rows / %d events, pv %d rows / %d events", out_dir, len(scada),
             len(events), len(pv), len(pv_events))


def write_sample(replay_dir: Path, out_dir: Path, wind_start: str, pv_start: str, days: float = 2.0) -> None:
    """Cut a small slice of the replay files (committed under tests/data for tests and the CI smoke test)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, start, col in (("wind", wind_start, "time"), ("wind_events", wind_start, "start"),
                             ("pv", pv_start, "time"), ("pv_events", pv_start, "start")):
        df = pd.read_parquet(replay_dir / f"{name}.parquet")
        t0 = pd.Timestamp(start, tz="UTC")
        df = df[(df[col] >= t0) & (df[col] < t0 + pd.Timedelta(days=days))]
        df.to_parquet(out_dir / f"{name}.parquet", index=False)
        log.info("sample %s: %d rows", name, len(df))
