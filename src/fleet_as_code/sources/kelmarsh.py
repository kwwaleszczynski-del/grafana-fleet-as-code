"""Kelmarsh wind farm SCADA (Greenbyte export, Zenodo record 16807551, CC BY 4.0).

One zip per year holds, for each of the 6 turbines, a 10-minute data file (~300 columns) and a status log of
stop / warning / informational events. This module reads the few channels the dashboards need and turns the
status log into a per-interval state code and time-based availability.

Usage:
    from fleet_as_code.sources import kelmarsh
    zips = kelmarsh.find_zips(Path("data/kelmarsh"), [2020])
    scada, events = kelmarsh.load(zips, "2020-10-15", "2020-11-15")
    scada = kelmarsh.add_state(scada, events)
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from fleet_as_code import config

log = logging.getLogger(__name__)

STEP = pd.Timedelta("10min")
COLUMNS = {
    "# Date and time": "time",
    "Wind speed Sensor 1 (m/s)": "ws1",
    "Wind speed Sensor 2 (m/s)": "ws2",
    "Power (kW)": "power_kw",
    "Energy Export (kWh)": "energy_kwh",
    "Rotor speed (RPM)": "rotor_rpm",
    "Blade angle (pitch position) A (\N{DEGREE SIGN})": "pitch_deg",
    "Nacelle ambient temperature (\N{DEGREE SIGN}C)": "ambient_temp_c",
    "Lost Production to Curtailment (Total) (kWh)": "curtailed_kwh",
}
STATUS_COLUMNS = {"Timestamp start": "start", "Timestamp end": "end", "Status": "status", "Code": "code",
                  "Message": "message", "IEC category": "category"}
_NAME = re.compile(r"(Turbine_Data|Status)_Kelmarsh_(\d)_")


def find_zips(data_dir: Path, years: list[int]) -> list[Path]:
    """Yearly archives as published on Zenodo, e.g. Kelmarsh_SCADA_2020_3086.zip."""
    zips = []
    for year in years:
        found = sorted(Path(data_dir).glob(f"Kelmarsh_SCADA_{year}_*.zip"))
        if not found:
            raise FileNotFoundError(f"no Kelmarsh_SCADA_{year}_*.zip in {data_dir}")
        zips.append(found[0])
    return zips


def _read_greenbyte(raw: bytes, header_prefix: str, usecols: list[str]) -> pd.DataFrame:
    """Greenbyte CSVs start with '#' comment lines; the header is the first line starting with header_prefix.
    The encoding differs between yearly exports (2019 is Latin-1, 2020 UTF-8), which matters for the degree sign."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    head = text[:4000].splitlines()
    skip = next(i for i, line in enumerate(head) if line.startswith(header_prefix))
    return pd.read_csv(io.StringIO(text), skiprows=skip, usecols=lambda c: c in usecols, na_values=["NaN", "-"])


def read_turbine(raw: bytes) -> pd.DataFrame:
    df = _read_greenbyte(raw, "# Date and time", list(COLUMNS)).rename(columns=COLUMNS)
    df["time"] = pd.to_datetime(df["time"]).dt.tz_localize("UTC")
    return df


def read_status(raw: bytes) -> pd.DataFrame:
    df = _read_greenbyte(raw, "Timestamp start", list(STATUS_COLUMNS)).rename(columns=STATUS_COLUMNS)
    for col in ("start", "end"):
        df[col] = pd.to_datetime(df[col]).dt.tz_localize("UTC")
    return df


def load(zips: list[Path], start: str, end: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """10-minute SCADA and stop/warning events of all turbines in [start, end), UTC, with an `asset` column."""
    t0, t1 = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
    scada, events = [], []
    for path in zips:
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                m = _NAME.match(Path(name).name)
                if not m:
                    continue
                asset = f"KWF{m.group(2)}"
                if m.group(1) == "Turbine_Data":
                    df = read_turbine(z.read(name))
                    scada.append(df[(df["time"] >= t0) & (df["time"] < t1)].assign(asset=asset))
                else:
                    df = read_status(z.read(name))
                    df = df[df["status"].isin(["Stop", "Warning"])]
                    # keep events that overlap the window (a stop can start before it)
                    end_ = df["end"].fillna(df["start"])
                    events.append(df[(end_ >= t0) & (df["start"] < t1)].assign(asset=asset))
        log.info("read %s", path.name)
    scada = pd.concat(scada, ignore_index=True).sort_values(["asset", "time"], ignore_index=True)
    events = pd.concat(events, ignore_index=True).sort_values(["start", "asset"], ignore_index=True)
    # the 'Wind speed' channel changes definition in April 2018; the mean of both anemometers is consistent
    scada["wind_speed"] = scada[["ws1", "ws2"]].mean(axis=1)
    log.info("Kelmarsh: %d rows, %d events, %s .. %s", len(scada), len(events), t0, t1)
    return scada.drop(columns=["ws1", "ws2"]), events


def stop_overlap(times: pd.Series, stops: pd.DataFrame, step: pd.Timedelta = STEP) -> np.ndarray:
    """Seconds of each interval [t, t + step) covered by the stop events (start, end); open stops last one step."""
    t = times.to_numpy(dtype="datetime64[ns]").astype("int64")
    out = np.zeros(len(t))
    starts = stops["start"].to_numpy(dtype="datetime64[ns]").astype("int64")
    ends = stops["end"].fillna(stops["start"] + step).to_numpy(dtype="datetime64[ns]").astype("int64")
    s = step.value
    for a, b in zip(starts, ends):
        lo, hi = np.searchsorted(t, a - s, "right"), np.searchsorted(t, b, "left")
        if hi > lo:
            seg = t[lo:hi]
            out[lo:hi] += np.clip(np.minimum(b, seg + s) - np.maximum(a, seg), 0, None)
    return np.minimum(out / 1e9, s / 1e9)


def add_state(scada: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    """Add `available` (fraction of the interval not in an unavailable stop) and `state` (config.STATES)."""
    out = []
    span = STEP.total_seconds()
    for asset, df in scada.groupby("asset", sort=True):
        stops = events[(events["asset"] == asset) & (events["status"] == "Stop")]
        unavailable = stops["category"].isin(config.UNAVAILABLE_CATEGORIES)
        maintenance = stops["category"].isin(config.MAINTENANCE_CATEGORIES)
        times = df["time"].reset_index(drop=True)
        down = stop_overlap(times, stops[unavailable]) / span
        maint = stop_overlap(times, stops[maintenance]) / span
        other = stop_overlap(times, stops[~unavailable]) / span
        producing = df["power_kw"].to_numpy() > config.PRODUCING_KW
        state = np.select([maint >= 0.5, down >= 0.5, other >= 0.5, producing], [3, 4, 2, 0], default=1)
        out.append(df.assign(available=1.0 - down, state=state))
    return pd.concat(out, ignore_index=True)
