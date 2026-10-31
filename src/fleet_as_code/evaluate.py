"""Evaluate the alert rules offline: the rule queries mirrored in pandas, run on the replay files the way Grafana
evaluates them during a replay, without InfluxDB or Grafana.

Evaluations run every EVAL_INTERVAL_S of wall time (10 plant minutes at 60x) from the replay start of each site;
each one sees the records in [t - window, t) like Flux's range(). Alert states follow Grafana's state machine:
a true condition goes Pending and fires once it has held for the rule's pending period (at once without one), a
false condition resets to Normal, and a missing series (no value, e.g. too few qualifying records) keeps its state
for one evaluation and is resolved on the second.

Two uses, both written to results/:
    alert_timing.csv, alert_episodes.csv     the demo replay (default starts, two injected faults) on the committed
                                             two-day sample: when each alert fires after its event, and every
                                             firing episode of the run
    underperformance_calibration.json        the first and the final wind underperformance rule on the unmodified
                                             month (`prepare` output), plus the lowest 2 h PV PR before the failure

Usage:
    python -m fleet_as_code alert-timing                        # tests/data -> results/alert_timing.csv
    python -m fleet_as_code calibrate --data data/replay        # needs the month from `prepare`
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from fleet_as_code import config
from fleet_as_code.grafana.alerts import EVAL_INTERVAL_S, Rule, rules
from fleet_as_code.reference import expected_power
from fleet_as_code.replay import Fault, Source, apply_faults

log = logging.getLogger(__name__)

RESULTS_DIR = config.ROOT / "results"
# The same values as the rules' Flux queries in grafana/flux.py (checked by the tests)
MIN_EXPECTED_KW, MIN_WIND_RECORDS = 500.0, 12
MIN_POA_WM2, MIN_PV_RECORDS = 300.0, 4
# First version of the wind underperformance rule, before tuning on the month: one fleet-wide reference curve,
# a 150 kW floor, a 2 h window with at least half of it (6 intervals) qualifying, and producing intervals of any
# availability (the "fully available" filter came later); threshold and pending period as in the final rule
FIRST_MIN_EXPECTED_KW, FIRST_WINDOW_H, FIRST_MIN_RECORDS = 150.0, 2.0, 6
# The demo run behind the README: the replay defaults and two injected faults
DEMO_WIND_START, DEMO_PV_START = "2020-10-24 02:00", "2016-06-04 04:00"
DEMO_FAULTS = ("derate:KWF4:3:12:0.7", "blackout:KWF2:11:3.5")
# Flux's now() is read when the query runs, a little after the scheduled evaluation time that fixes the range
# (about 2 s of wall time in the portable run, see results/grafana_state_history.csv), so a staleness of exactly
# 30 plant minutes already counts as more than 30
NOW_LAG_WALL_S = 2.0
EPOCH = pd.Timestamp("1970-01-01", tz="UTC")


def _seconds(times: pd.Series) -> np.ndarray:
    return (times - EPOCH).dt.total_seconds().to_numpy()


def _stamp(seconds: float) -> pd.Timestamp:
    return EPOCH + pd.Timedelta(seconds=float(seconds))


def window_sum(t: np.ndarray, values: np.ndarray, at: np.ndarray, window_s: float) -> np.ndarray:
    """Sum of values over the records with time in [a - window_s, a) for each evaluation time a (t sorted)."""
    c = np.concatenate([[0.0], np.cumsum(values)])
    return c[np.searchsorted(t, at, "left")] - c[np.searchsorted(t, at - window_s, "left")]


def _ratio(num: np.ndarray, den: np.ndarray, ok: np.ndarray) -> np.ndarray:
    return np.where(ok & (den > 0), num / np.where(den > 0, den, 1.0), np.nan)


def wind_performance(g: pd.DataFrame, at: np.ndarray, window_s: float, min_expected: float = MIN_EXPECTED_KW,
                     min_records: int = MIN_WIND_RECORDS, expected: str = "expected_kw",
                     fully_available: bool = True) -> np.ndarray:
    """flux.performance_index: sum(power) / sum(expected) over fully available producing intervals expecting more
    than min_expected kW, at least min_records of them."""
    q = qualifying(g, min_expected, expected, fully_available)
    t = _seconds(g["time"])
    actual = window_sum(t, np.where(q, g["power_kw"].to_numpy(), 0.0), at, window_s)
    exp = window_sum(t, np.where(q, g[expected].to_numpy(), 0.0), at, window_s)
    return _ratio(actual, exp, window_sum(t, q.astype(float), at, window_s) >= min_records)


def qualifying(g: pd.DataFrame, min_expected: float, expected: str, fully_available: bool = True) -> np.ndarray:
    """Intervals the wind underperformance rule sums over (the filter steps of flux.performance_index)."""
    available = g["available"] >= 1.0 if fully_available else g["available"].notna()
    return ((g["state"] == 0) & available & (g[expected] > min_expected) & g["power_kw"].notna()).to_numpy()


def pv_performance_ratio(g: pd.DataFrame, at: np.ndarray, window_s: float) -> np.ndarray:
    """flux.performance_ratio: E_AC / (P_STC x H_POA / 1000 W/m2) over records with POA above MIN_POA_WM2."""
    q = (g["ac_kw"].notna() & (g["poa_wm2"] > MIN_POA_WM2)).to_numpy()
    t = _seconds(g["time"])
    e = window_sum(t, np.where(q, g["ac_kw"].to_numpy(), 0.0), at, window_s)
    h = window_sum(t, np.where(q, g["poa_wm2"].to_numpy() / 1000.0 * config.PV.pdc_kwp, 0.0), at, window_s)
    return _ratio(e, h, window_sum(t, q.astype(float), at, window_s) >= MIN_PV_RECORDS)


def staleness(g: pd.DataFrame, at: np.ndarray, window_s: float,
              lag_s: float = NOW_LAG_WALL_S * config.SPEED) -> np.ndarray:
    """flux.staleness: plant minutes from the last power record in the window to now() (lag_s after at)."""
    power = "power_kw" if "power_kw" in g else "ac_kw"
    t = _seconds(g.loc[g[power].notna(), "time"])
    i = np.searchsorted(t, at, "left") - 1
    last = np.where(i >= 0, t[np.maximum(i, 0)] if len(t) else np.nan, np.nan)
    return np.where(last >= at - window_s, (at + lag_s - last) / 60.0, np.nan)


def availability(g: pd.DataFrame, at: np.ndarray, window_s: float) -> np.ndarray:
    """flux.availability: mean of the `available` records in the window."""
    ok = g["available"].notna().to_numpy()
    t = _seconds(g["time"])
    s = window_sum(t, np.where(ok, g["available"].to_numpy(), 0.0), at, window_s)
    n = window_sum(t, ok.astype(float), at, window_s)
    return _ratio(s, n, n > 0)


# rule uid -> (measurements it reads, value function)
VALUES = {
    "wind-underperformance": (("wind_turbine",), wind_performance),
    "pv-underperformance": (("pv_inverter",), pv_performance_ratio),
    "no-data": (("wind_turbine", "pv_inverter"), staleness),
    "availability-drop": (("wind_turbine", "pv_inverter"), availability),
}


def episodes(at: np.ndarray, values: np.ndarray, op: str, threshold: float, pending_s: float) -> list[tuple]:
    """Firing episodes (fired, resolved or NaN) of one alert instance from its values at the evaluation times."""
    out, state, since, missing = [], "Normal", 0.0, 0
    for a, v in zip(at, values):
        if np.isnan(v):
            missing += 1
            if missing >= 2:
                if state == "Alerting":
                    out[-1] = (out[-1][0], a)
                state = "Normal"
            continue
        missing = 0
        if (v < threshold) if op == "lt" else (v > threshold):
            if state == "Normal":
                state, since = "Pending", a
            if state == "Pending" and a - since >= pending_s:
                state = "Alerting"
                out.append((a, np.nan))
        else:
            if state == "Alerting":
                out[-1] = (out[-1][0], a)
            state = "Normal"
    return out


def evaluate(sources: list[Source], speed: float = config.SPEED, rule_set: list[Rule] | None = None,
             faults: list[Fault] = ()) -> pd.DataFrame:
    """Every firing episode (rule, asset, fired, resolved; UTC) from the replay start of each source to the end of
    its data, with the faults applied as in the replay."""
    step = EVAL_INTERVAL_S * speed
    rows = []
    for src in sources:
        offset_h = (_seconds(src.data["time"]) - src.start.timestamp()) / 3600
        data = apply_faults(src.data, offset_h, list(faults)).sort_values(["asset", "time"])
        start = src.start.timestamp()
        at = start + step * np.arange(int((_seconds(data["time"]).max() - start) // step) + 1)
        for r in rule_set or rules(speed):
            measurements, fn = VALUES[r.uid]
            if src.measurement not in measurements:
                continue
            window = config.plant_to_wall(r.window_h * 3600, speed) * speed
            pending = 0 if r.for_h == 0 else config.plant_to_wall(r.for_h * 3600, speed) * speed
            for asset, g in data.groupby("asset"):
                for fired, resolved in episodes(at, fn(g, at, window), r.op, r.threshold, pending):
                    rows.append({"rule": r.title, "asset": asset, "fired": _stamp(fired),
                                 "resolved": _stamp(resolved) if not np.isnan(resolved) else pd.NaT})
    return pd.DataFrame(rows, columns=["rule", "asset", "fired", "resolved"])


@dataclass(frozen=True)
class Event:
    event: str
    origin: str             # "real" or "injected"
    asset: str
    start: pd.Timestamp
    rules: tuple[str, ...]  # rule titles expected to fire


def demo_events(wind: Source, pv: Source, faults: list[Fault]) -> list[Event]:
    """The injected faults and the two real failures of the demo, located in the data."""
    titles = {r.uid: r.title for r in rules()}
    out = []
    for f in faults:
        src = wind if f.asset in {t.asset for t in config.TURBINES} else pv
        start = src.start + pd.Timedelta(hours=f.start_h)
        if f.kind == "derate":
            out.append(Event(f"{f.asset} derated to {f.factor:.0%} for {f.hours:g} h", "injected", f.asset, start,
                             (titles["wind-underperformance"],)))
        else:
            out.append(Event(f"{f.asset} stops sending data for {f.hours:g} h", "injected", f.asset, start,
                             (titles["no-data"],)))
    d = pv.data
    off = d[(d["time"] >= pv.start) & (d["available"] == 0)].iloc[0]
    out.append(Event(f"{config.PV.asset} stops producing in daylight, error code {off['error_code']:.0f}", "real",
                     config.PV.asset, off["time"], (titles["availability-drop"], titles["pv-underperformance"])))
    ev = wind.events
    stop = ev[(ev["asset"] == "KWF3") & (ev["status"] == "Stop") & (ev["start"] >= wind.start)].iloc[0]
    out.append(Event(f"KWF3 {stop['message'].lower()}", "real", "KWF3", stop["start"], (titles["availability-drop"],)))
    return out


def load_sources(data_dir: Path, wind_start: str, pv_start: str) -> tuple[Source, Source]:
    d = Path(data_dir)
    return (Source.wind(pd.read_parquet(d / "wind.parquet"), pd.read_parquet(d / "wind_events.parquet"), wind_start),
            Source.pv(pd.read_parquet(d / "pv.parquet"), pd.read_parquet(d / "pv_events.parquet"), pv_start))


def alert_timing(data_dir: Path = config.SAMPLE_DIR, wind_start: str = DEMO_WIND_START,
                 pv_start: str = DEMO_PV_START, faults=DEMO_FAULTS) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(timing, episodes): for each event and expected rule the first firing at or after the event start, and
    all firing episodes of the run."""
    wind, pv = load_sources(data_dir, wind_start, pv_start)
    faults = [Fault.parse(f) for f in faults]
    eps = evaluate([wind, pv], faults=faults)
    rows = []
    for e in demo_events(wind, pv, faults):
        for title in e.rules:
            hit = eps[(eps["rule"] == title) & (eps["asset"] == e.asset) & (eps["fired"] >= e.start)]
            fired = hit["fired"].min() if len(hit) else pd.NaT
            rows.append({"event": e.event, "origin": e.origin, "rule": title, "asset": e.asset, "event_start": e.start,
                         "fired": fired, "fired_after_h": round((fired - e.start).total_seconds() / 3600, 2)})
    return pd.DataFrame(rows), eps


def fleet_curve(curves: pd.DataFrame, bin_width: float = 0.5) -> pd.DataFrame:
    """One curve for the whole fleet: the per-turbine curves pooled bin by bin, weighted by their sample counts."""
    c = curves.assign(bin=np.floor(curves["wind_speed"] / bin_width).astype(int),
                      ws_n=curves["wind_speed"] * curves["samples"], p_n=curves["power_kw"] * curves["samples"])
    g = c.groupby("bin")[["ws_n", "p_n", "samples"]].sum()
    return pd.DataFrame({"wind_speed": g["ws_n"] / g["samples"], "power_kw": g["p_n"] / g["samples"],
                         "samples": g["samples"]}).reset_index(drop=True)


def calibrate(data_dir: Path, speed: float = config.SPEED) -> dict:
    """The first and the final wind underperformance rule over the whole unmodified month (evaluations from the
    first to the last record), and the lowest 2 h PV PR before the inverter failure."""
    wind = pd.read_parquet(Path(data_dir) / "wind.parquet")
    wind["fleet_expected_kw"] = expected_power(wind["wind_speed"], fleet_curve(pd.read_csv(config.REFERENCE_CURVE)))
    final = next(r for r in rules(speed) if r.uid == "wind-underperformance")
    step = EVAL_INTERVAL_S * speed
    t0, t1 = _seconds(wind["time"]).min(), _seconds(wind["time"]).max()
    at = t0 + step * np.arange(int((t1 - t0) // step) + 1)
    pending = config.plant_to_wall(final.for_h * 3600, speed) * speed
    out = {"period": f"{_stamp(t0):%Y-%m-%d} .. {_stamp(t1):%Y-%m-%d}"}
    versions = {"first": ("fleet_expected_kw", FIRST_MIN_EXPECTED_KW, FIRST_WINDOW_H, FIRST_MIN_RECORDS, False),
                "final": ("expected_kw", MIN_EXPECTED_KW, final.window_h, MIN_WIND_RECORDS, True)}
    for name, (expected, floor, window_h, min_records, fully_available) in versions.items():
        window = config.plant_to_wall(window_h * 3600, speed) * speed
        per_asset, ws = {}, []
        for asset, g in wind.sort_values(["asset", "time"]).groupby("asset"):
            v = wind_performance(g, at, window, floor, min_records, expected, fully_available)
            eps = episodes(at, v, final.op, final.threshold, pending)
            per_asset[asset] = len(eps)
            q = qualifying(g, floor, expected, fully_available)
            t = _seconds(g["time"])
            for fired, _ in eps:   # mean wind speed of the qualifying intervals behind each firing
                m = q & (t >= fired - window) & (t < fired)
                ws.append(float(g["wind_speed"].to_numpy()[m].mean()))
        ws = np.array(ws)
        out[name] = {"reference_curve": "fleet" if expected == "fleet_expected_kw" else "per turbine",
                     "min_expected_kw": floor, "window_h": window_h, "min_records": min_records,
                     "fully_available_only": fully_available, "episodes": int(sum(per_asset.values())),
                     "episodes_by_asset": per_asset,
                     "episodes_at_4_5_to_6_ms": int(((ws >= 4.5) & (ws <= 6.0)).sum())}
    ok = (wind["state"] == 0) & (wind["available"] >= 1.0) & (wind["fleet_expected_kw"] > 0)
    by_asset = wind[ok].groupby("asset")[["power_kw", "fleet_expected_kw"]].sum()
    out["energy_vs_fleet_curve"] = (by_asset["power_kw"] / by_asset["fleet_expected_kw"]).round(3).to_dict()

    pv = pd.read_parquet(Path(data_dir) / "pv.parquet")
    stop = pv.loc[pv["available"] == 0, "time"].min()
    pr_rule = next(r for r in rules(speed) if r.uid == "pv-underperformance")
    window = config.plant_to_wall(pr_rule.window_h * 3600, speed) * speed
    at = _seconds(pv.loc[pv["time"] <= stop, "time"])     # every distinct 2 h window up to the stop
    pr = pv_performance_ratio(pv, at, window)
    out["pv_before_failure"] = {"stop": f"{stop:%Y-%m-%d %H:%M} UTC", "min_pr_2h": round(float(np.nanmin(pr)), 2),
                                "below_threshold": bool((pr < pr_rule.threshold).any())}
    return out


def write_alert_timing(data_dir: Path, out_dir: Path = RESULTS_DIR) -> pd.DataFrame:
    timing, eps = alert_timing(data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    timing.to_csv(out_dir / "alert_timing.csv", index=False, lineterminator="\n")
    eps.to_csv(out_dir / "alert_episodes.csv", index=False, lineterminator="\n")
    for r in timing.itertuples():
        log.info("%-45s %-30s %-5s fired after %.2f h", r.event, r.rule, r.asset, r.fired_after_h)
    log.info("%d firing episodes in the run -> %s", len(eps), out_dir)
    return timing


def write_calibration(data_dir: Path, out_dir: Path = RESULTS_DIR) -> dict:
    out = calibrate(data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "underperformance_calibration.json").write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    log.info("underperformance episodes in %s: first rule %d, final rule %d; lowest 2 h PV PR before the failure "
             "%.2f", out["period"], out["first"]["episodes"], out["final"]["episodes"],
             out["pv_before_failure"]["min_pr_2h"])
    return out
