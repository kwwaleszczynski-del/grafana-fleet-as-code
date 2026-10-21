"""Reference power curve by the method of bins (IEC 61400-12-1 style, without air density correction).

One curve per turbine is built from a reference year of normal operation and stored in
reference/kelmarsh_power_curves.csv. The replay uses it to add an `expected_kw` channel (the power the turbine
should produce at the measured wind speed, like a SCADA "possible power" signal); the turbine dashboard plots it
behind the live scatter.

Usage:
    from fleet_as_code.reference import curves_by_asset, expected_by_asset
    curves = curves_by_asset(scada_2019)       # columns asset, wind_speed, power_kw, samples
    scada["expected_kw"] = expected_by_asset(scada, curves)
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def power_curve(scada: pd.DataFrame, bin_width: float = 0.5, min_samples: int = 10) -> pd.DataFrame:
    """Mean wind speed and power per wind speed bin over intervals of normal operation: fully available, not
    stopped (state 0 or 1) and not curtailed. Bins with fewer than min_samples intervals are dropped."""
    ok = (scada["available"] >= 1.0) & scada["state"].isin([0, 1]) & scada["wind_speed"].notna() \
        & scada["power_kw"].notna() & (scada["curtailed_kwh"].fillna(0) <= 0)
    d = scada.loc[ok, ["wind_speed", "power_kw"]]
    bins = np.floor(d["wind_speed"] / bin_width).astype(int)
    curve = d.groupby(bins).agg(wind_speed=("wind_speed", "mean"), power_kw=("power_kw", "mean"),
                                samples=("power_kw", "size"))
    curve = curve[curve["samples"] >= min_samples].reset_index(drop=True)
    curve["power_kw"] = curve["power_kw"].clip(lower=0)
    return curve


def expected_power(wind_speed: pd.Series, curve: pd.DataFrame) -> np.ndarray:
    """Linear interpolation of the curve; zero below its first bin, last bin's power above its last bin."""
    ws = np.asarray(wind_speed, dtype=float)
    out = np.interp(ws, curve["wind_speed"], curve["power_kw"], left=0.0, right=float(curve["power_kw"].iloc[-1]))
    return np.where(np.isnan(ws), np.nan, out)


def curves_by_asset(scada: pd.DataFrame, **kw) -> pd.DataFrame:
    """One curve per turbine (columns asset, wind_speed, power_kw, samples): each turbine is judged against its
    own history, so site effects such as wakes or a slightly biased anemometer do not read as underperformance."""
    return pd.concat([power_curve(g, **kw).assign(asset=a) for a, g in scada.groupby("asset")], ignore_index=True)[
        ["asset", "wind_speed", "power_kw", "samples"]]


def expected_by_asset(scada: pd.DataFrame, curves: pd.DataFrame) -> np.ndarray:
    out = np.full(len(scada), np.nan)
    for asset, curve in curves.groupby("asset"):
        m = (scada["asset"] == asset).to_numpy()
        out[m] = expected_power(scada.loc[m, "wind_speed"], curve)
    return out
