import numpy as np
import pandas as pd
import pytest

from fleet_as_code import config
from fleet_as_code.reference import curves_by_asset, expected_by_asset, expected_power, power_curve


def test_method_of_bins_filters_abnormal_operation():
    rng = np.random.default_rng(0)
    ws = rng.uniform(0, 20, 20000)
    power = np.clip((ws - 3) ** 3 * 5, 0, 2000)
    df = pd.DataFrame({"wind_speed": ws, "power_kw": power, "available": 1.0, "state": 0, "curtailed_kwh": 0.0})
    df.loc[:999, ["power_kw", "state"]] = [0.0, 4]           # stopped turbine: must not drag the curve down
    df.loc[1000:1999, "curtailed_kwh"] = 50.0
    df.loc[1000:1999, "power_kw"] = 100.0
    curve = power_curve(df)
    assert curve["wind_speed"].is_monotonic_increasing and curve["power_kw"].max() == 2000
    mid = curve.iloc[(curve["wind_speed"] - 10).abs().argmin()]
    assert abs(mid["power_kw"] - (mid["wind_speed"] - 3) ** 3 * 5) < 20


def test_expected_power_edges():
    curve = pd.DataFrame({"wind_speed": [3.0, 4.0, 12.0], "power_kw": [0.0, 100.0, 2000.0], "samples": 10})
    out = expected_power(pd.Series([1.0, 3.5, 20.0, np.nan]), curve)
    assert out[0] == 0 and out[1] == 50 and out[2] == 2000 and np.isnan(out[3])


def test_curves_by_asset_and_lookup():
    curves = pd.DataFrame({"asset": ["A", "A", "B", "B"], "wind_speed": [4.0, 12.0, 4.0, 12.0],
                           "power_kw": [0.0, 2000.0, 0.0, 1000.0], "samples": 10})
    scada = pd.DataFrame({"asset": ["A", "B", "C"], "wind_speed": [8.0, 8.0, 8.0]})
    out = expected_by_asset(scada, curves)
    assert out[0] == 1000 and out[1] == 500 and np.isnan(out[2])      # no curve, no expectation
    df = pd.DataFrame({"asset": ["A"] * 20 + ["B"] * 20, "wind_speed": [5.1] * 40, "power_kw": [300.0] * 40,
                       "available": 1.0, "state": 0, "curtailed_kwh": 0.0})
    assert list(curves_by_asset(df)["asset"]) == ["A", "B"]


@pytest.mark.parametrize("asset", [t.asset for t in config.TURBINES])
def test_committed_reference_curves_are_plausible(asset):
    curves = pd.read_csv(config.REFERENCE_CURVE)
    assert list(curves.columns) == ["asset", "wind_speed", "power_kw", "samples"]
    curve = curves[curves["asset"] == asset]
    assert curve["wind_speed"].is_monotonic_increasing and (curve["samples"] >= 10).all()
    assert 1900 < curve["power_kw"].max() <= 1.02 * config.TURBINES[0].rated_kw   # 10-min means can exceed rating
    below_cut_in = curve[curve["wind_speed"] < 2.5]["power_kw"]
    assert (below_cut_in < 5).all()
    rising = curve[curve["wind_speed"] < 11]["power_kw"].to_numpy()
    assert (np.diff(rising) >= 0).all()
