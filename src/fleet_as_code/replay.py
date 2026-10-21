"""Replay historical SCADA into InfluxDB as a live stream on an accelerated clock.

Each source has its own historical start (wind and PV come from different years and continents). At wall time t0
both starts map to "now"; a record `dt` seconds of plant time after its start is written with the timestamp
t0 + dt / speed, as soon as that moment has passed. `backfill_h` hours of plant time before the start are written
at once so the dashboards open with history. Optional fault injection makes the alert rules fire on demand.

Usage:
    from fleet_as_code.replay import Fault, Source, build_lines, run
    src = Source.wind(pd.read_parquet("data/replay/wind.parquet"), events, start="2020-10-24 02:00")
    times, lines = build_lines([src], speed=60, t0=time.time(), backfill_h=48, duration_h=96,
                               faults=[Fault.parse("derate:KWF4:2:12:0.7")])
    run(Influx.from_env(), times, lines, [src], speed=60, t0=t0)
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from fleet_as_code import config
from fleet_as_code.influx import Influx, line

log = logging.getLogger(__name__)

WIND_FIELDS = ("power_kw", "expected_kw", "wind_speed", "rotor_rpm", "pitch_deg", "ambient_temp_c", "energy_kwh",
               "available", "state")
PV_FIELDS = ("ac_kw", "dc_kw", "poa_wm2", "module_temp_c", "ambient_temp_c", "energy_kwh", "available", "state",
             "error_code")
INT_FIELDS = {"state", "error_code"}


@dataclass(frozen=True)
class Fault:
    """Injected fault, relative to the replay start in plant hours. kind: 'blackout' drops the asset's records
    (communication loss), 'derate' multiplies its power and energy by factor (e.g. a pitch or yaw problem)."""
    kind: str
    asset: str
    start_h: float
    hours: float
    factor: float = 1.0

    @classmethod
    def parse(cls, spec: str) -> "Fault":
        """'blackout:KWF2:6:3' or 'derate:KWF4:2:12:0.7'"""
        parts = spec.split(":")
        if len(parts) not in (4, 5) or parts[0] not in ("blackout", "derate"):
            raise ValueError(f"bad fault spec {spec!r}, expected kind:asset:start_h:hours[:factor]")
        return cls(parts[0], parts[1], float(parts[2]), float(parts[3]), float(parts[4]) if len(parts) == 5 else 1.0)


@dataclass
class Source:
    site: str
    measurement: str
    data: pd.DataFrame          # time (UTC), asset, fields
    events: pd.DataFrame        # start (UTC), asset, status, code, message, category
    start: pd.Timestamp         # historical instant that maps to the replay start
    fields: tuple = field(default=())

    @classmethod
    def wind(cls, data, events, start) -> "Source":
        return cls(config.WIND_SITE, "wind_turbine", data, events, pd.Timestamp(start, tz="UTC"), WIND_FIELDS)

    @classmethod
    def pv(cls, data, events, start) -> "Source":
        data = data.assign(asset=config.PV.asset) if "asset" not in data else data
        return cls(config.PV_SITE, "pv_inverter", data, events, pd.Timestamp(start, tz="UTC"), PV_FIELDS)


def apply_faults(df: pd.DataFrame, offset_h: np.ndarray, faults: list[Fault]) -> pd.DataFrame:
    keep = np.ones(len(df), dtype=bool)
    df = df.copy()
    for f in faults:
        hit = (df["asset"].to_numpy() == f.asset) & (offset_h >= f.start_h) & (offset_h < f.start_h + f.hours)
        if f.kind == "blackout":
            keep &= ~hit
        else:
            for col in ("power_kw", "ac_kw", "dc_kw", "energy_kwh"):
                if col in df:
                    df.loc[hit, col] = df.loc[hit, col] * f.factor
    return df[keep]


def _offsets(times: pd.Series, start: pd.Timestamp) -> np.ndarray:
    return (times - start).dt.total_seconds().to_numpy()


def build_lines(sources: list[Source], speed: float, t0: float, backfill_h: float, duration_h: float,
                faults: list[Fault] = ()) -> tuple[np.ndarray, list[str]]:
    """Wall-clock write times (s, sorted) and line protocol (with ms timestamps) for all records in
    [start - backfill_h, start + duration_h) of plant time."""
    rows = []
    for src in sources:
        offset = _offsets(src.data["time"], src.start)
        inside = (offset >= -backfill_h * 3600) & (offset < duration_h * 3600)
        df = apply_faults(src.data[inside], offset[inside] / 3600, list(faults))
        wall = t0 + _offsets(df["time"], src.start) / speed
        tags = {"site": src.site}
        for w, rec in zip(wall, df[["asset", *src.fields]].to_dict("records")):
            fields = {k: (int(v) if k in INT_FIELDS and pd.notna(v) else v) for k, v in rec.items() if k != "asset"}
            lp = line(src.measurement, {**tags, "asset": rec["asset"]}, fields, ts_ms=round(w * 1000))
            if lp:
                rows.append((w, lp))
        ev = src.events
        offset = _offsets(ev["start"], src.start)
        ev = ev[(offset >= -backfill_h * 3600) & (offset < duration_h * 3600)]
        for w, rec in zip(t0 + _offsets(ev["start"], src.start) / speed, ev.to_dict("records")):
            lp = line("event", {**tags, "asset": rec["asset"], "severity": rec["status"], "category": rec["category"] if isinstance(rec["category"], str) else "Unspecified"},
                      {"code": int(rec["code"]), "message": str(rec["message"])}, ts_ms=round(w * 1000))
            rows.append((w, lp))
    rows.sort(key=lambda r: r[0])
    return np.array([r[0] for r in rows]), [r[1] for r in rows]


SITE_TZ = {config.WIND_SITE: ("UTC", "UTC"), config.PV_SITE: (config.PV.tz, "UTC-8")}


def plant_time(src: Source, speed: float, t0: float, now: float) -> pd.Timestamp:
    return src.start + pd.Timedelta(seconds=(now - t0) * speed)


def heartbeat(sources: list[Source], speed: float, t0: float, now: float) -> list[str]:
    """Current plant clock per site (also as local-time text), shown on the overview dashboard."""
    out = []
    for s in sources:
        t = plant_time(s, speed, t0, now)
        tz, label = SITE_TZ.get(s.site, ("UTC", "UTC"))
        out.append(line("replay", {"site": s.site}, {"source_time_ms": int(t.value // 10**6),
                                                      "plant_clock": f"{t.tz_convert(tz):%Y-%m-%d %H:%M} {label}",
                                                      "speed": float(speed)}, ts_ms=round(now * 1000)))
    return out


def run(db: Influx, times: np.ndarray, lines: list[str], sources: list[Source], speed: float, t0: float,
        tick: float = 1.0, clock=time.time, sleep=time.sleep) -> int:
    """Write every line once its wall time has passed; returns the number of lines written."""
    i, written, last_log = 0, 0, clock()
    while i < len(lines):
        now = clock()
        j = int(np.searchsorted(times, now, side="right"))
        batch = lines[i:j] + heartbeat(sources, speed, t0, now)
        db.write(config.BUCKET, batch)
        written += j - i
        i = j
        if now - last_log >= 60 or i == len(lines):
            clocks = ", ".join(f"{s.site} {plant_time(s, speed, t0, now):%Y-%m-%d %H:%M}" for s in sources)
            log.info("%d / %d lines written, plant clock: %s", i, len(lines), clocks)
            last_log = now
        if i < len(lines):
            sleep(max(0.0, min(tick, times[i] - clock())))
    return written
