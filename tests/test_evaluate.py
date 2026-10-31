"""The alert timings and calibration counts quoted in the README come from results/; the offline evaluation of the
alert rules reproduces those files from the committed sample and Grafana's own state history of the portable run
behind the screenshots."""
import io
import json
import re

import numpy as np
import pandas as pd
import pytest

from fleet_as_code import config
from fleet_as_code.evaluate import (DEMO_FAULTS, DEMO_PV_START, DEMO_WIND_START, MIN_EXPECTED_KW, MIN_POA_WM2,
                                    MIN_PV_RECORDS, MIN_WIND_RECORDS, NOW_LAG_WALL_S, RESULTS_DIR, VALUES, alert_timing,
                                    episodes, load_sources)
from fleet_as_code.grafana.alerts import rules
from fleet_as_code.replay import Fault, apply_faults

README = (config.ROOT / "README.md").read_text(encoding="utf-8")
TIMING = pd.read_csv(RESULTS_DIR / "alert_timing.csv")
CALIBRATION = json.loads((RESULTS_DIR / "underperformance_calibration.json").read_text(encoding="utf-8"))
RULES = {r.title: r for r in rules()}


@pytest.fixture(scope="module")
def recomputed():
    return alert_timing(config.SAMPLE_DIR)


def _roundtrip(df: pd.DataFrame) -> pd.DataFrame:
    return pd.read_csv(io.StringIO(df.to_csv(index=False)))


def test_pandas_rules_use_the_flux_thresholds():
    q = {r.uid: r.query for r in rules()}
    assert f"r.expected_kw > {MIN_EXPECTED_KW:.1f})" in q["wind-underperformance"]
    assert f"r.n >= {MIN_WIND_RECORDS} " in q["wind-underperformance"]
    assert f"r.poa_wm2 > {MIN_POA_WM2:.1f})" in q["pv-underperformance"]
    assert f"r.n >= {MIN_PV_RECORDS} " in q["pv-underperformance"]


def test_state_machine_pending_and_missing_series():
    at = np.arange(10) * 600.0
    v = np.array([1, 0, 0, 0, 0, np.nan, 0, np.nan, np.nan, 0], dtype=float)
    # pending from 600 s, fires 1800 s later; one missing evaluation keeps the state, the second resolves it
    assert episodes(at, v, "lt", 0.5, 1800) == [(2400.0, 4800.0)]
    eps = episodes(at, v, "lt", 0.5, 0)
    assert [e[0] for e in eps] == [600.0, 5400.0] and eps[0][1] == 4800.0 and np.isnan(eps[1][1])


def test_alert_timing_is_reproducible(recomputed):
    timing, eps = recomputed
    pd.testing.assert_frame_equal(_roundtrip(timing), TIMING)
    pd.testing.assert_frame_equal(_roundtrip(eps), pd.read_csv(RESULTS_DIR / "alert_episodes.csv"))
    assert timing["fired"].notna().all()


def test_no_other_alert_fires_in_the_demo_run(recomputed):
    timing, eps = recomputed
    assert sorted(zip(eps["rule"], eps["asset"], eps["fired"])) == sorted(zip(timing["rule"], timing["asset"],
                                                                              timing["fired"]))


def test_readme_alert_table_matches_results():
    section = README.split("## What the replay shows")[1].split("\n## ")[0]
    rows = re.findall(r"^\|[^|]+\|[^|]*\| (.+?) \| ([\d.]+) h \|$", section, re.M)
    expected = [(f"{r.rule} ({r.asset})", f"{r.fired_after_h:.1f}") for r in TIMING.itertuples()]
    assert sorted(rows) == sorted(expected)


def test_readme_text_quotes_results():
    first, final, pv = CALIBRATION["first"], CALIBRATION["final"], CALIBRATION["pv_before_failure"]
    after = {(r.rule, r.asset): r.fired_after_h for r in TIMING.itertuples()}
    text = " ".join(README.split())
    assert first["episodes"] > final["episodes"]
    assert f"would have fired {first['episodes']} times in the month" in text
    assert f"{first['episodes_at_4_5_to_6_ms']} of them at 4.5-6 m/s" in text
    assert f"the largest share ({first['episodes_by_asset']['KWF1']}) on KWF1" in text
    assert max(first["episodes_by_asset"], key=first["episodes_by_asset"].get) == "KWF1"
    kwf1_gap = round(100 * (1 - CALIBRATION["energy_vs_fleet_curve"]["KWF1"]))
    assert f"runs about {kwf1_gap} % below the fleet curve" in text
    assert f"bring that down to {final['episodes']} episodes" in text
    assert f"caught about {after[('Wind turbine underperformance', 'KWF4')]:.1f} h after it starts" in text
    assert f"catches it {after[('PV underperformance', 'INV1')]:.1f} h after the stop" in text
    assert f"(lowest 2 h PR {pv['min_pr_2h']:.2f})" in text and not pv["below_threshold"]


def test_offline_evaluation_reproduces_grafana_state_history(recomputed):
    """Same transitions at the same evaluations with the same values as the portable run behind the screenshots
    (staleness differs by the few seconds between the scheduled evaluation and Flux's now())."""
    hist = pd.read_csv(RESULTS_DIR / "grafana_state_history.csv", parse_dates=["wall_time"])
    _, eps = recomputed
    start = {"INV1": pd.Timestamp(DEMO_PV_START, tz="UTC")}
    wind_start = pd.Timestamp(DEMO_WIND_START, tz="UTC")
    rows = []
    for e in eps.itertuples():
        pending = pd.Timedelta(seconds=config.plant_to_wall(RULES[e.rule].for_h * 3600) * config.SPEED)
        if RULES[e.rule].for_h > 0:
            rows.append((e.rule, e.asset, "Pending", e.fired - pending))
        rows.append((e.rule, e.asset, "Alerting", e.fired))
        if pd.notna(e.resolved):
            rows.append((e.rule, e.asset, "Normal", e.resolved))
    # plant time -> seconds of wall time after the replay start (one episode per rule and asset in this run),
    # anchored on the first recorded transition
    offline = {(rule, asset, state): (t - start.get(asset, wind_start)).total_seconds() / config.SPEED
               for rule, asset, state, t in rows}
    first = hist.iloc[0]
    t0 = first["wall_time"] - pd.Timedelta(seconds=offline[(first["rule"], first["asset"], first["new_state"])])
    in_run = {k: t0 + pd.Timedelta(seconds=s) for k, s in offline.items()
              if t0 + pd.Timedelta(seconds=s) <= hist["wall_time"].max()}
    assert in_run == {(r.rule, r.asset, r.new_state): r.wall_time for r in hist.itertuples()}

    wind, pv = load_sources(config.SAMPLE_DIR, DEMO_WIND_START, DEMO_PV_START)
    faults = [Fault.parse(f) for f in DEMO_FAULTS]
    for r in hist[hist["new_state"] != "Pending"].itertuples():
        src = pv if r.asset == config.PV.asset else wind
        offset_h = (src.data["time"] - src.start).dt.total_seconds().to_numpy() / 3600
        g = apply_faults(src.data, offset_h, faults)
        g = g[g["asset"] == r.asset].sort_values("time")
        at = src.start + (r.wall_time - t0) * config.SPEED
        rule = RULES[r.rule]
        value = VALUES[rule.uid][1](g, np.array([at.timestamp()]), rule.window_h * 3600)[0]
        if rule.uid == "no-data":
            assert 0 < r.value - (value - NOW_LAG_WALL_S * config.SPEED / 60) < 10
        else:
            assert value == pytest.approx(r.value, abs=1e-6)
