import io
import zipfile

import numpy as np
import pandas as pd
import pytest

from fleet_as_code import config
from fleet_as_code.sources import kelmarsh, pvdaq

HEADER = ("# This file was exported by Greenbyte\n#\n# Turbine: Kelmarsh 1\n# Turbine type: Senvion MM92\n"
          "# Time zone: UTC\n#\n#\n#\n#\n")


def turbine_csv(encoding: str) -> bytes:
    cols = list(kelmarsh.COLUMNS) + ["Unused (m/s)"]
    rows = ["2020-10-24 00:00:00,5.0,5.2,400,66,12.1,1.5,9.5,0,1",
            "2020-10-24 00:10:00,6.0,NaN,600,100,13.0,0.5,9.4,0,1",
            "2020-10-24 00:20:00,7.0,7.0,800,133,14.0,0.0,9.3,0,1"]
    return (HEADER + ",".join(cols) + "\n" + "\n".join(rows) + "\n").encode(encoding)


def status_csv() -> bytes:
    head = HEADER.replace("#\n#\n#\n", "# Kelmarsh 1 Sum production: 1 kWh\n#\n")
    cols = "Timestamp start,Timestamp end,Duration,Status,Code,Message,Comment,Service contract category,IEC category"
    rows = ["2020-10-24 00:05:00,2020-10-24 00:25:00,00:20:00,Stop,3110,Frequency converter error,,x,Forced outage",
            "2020-10-24 00:12:00,-,-,Informational,0,System OK,,,Full Performance"]
    return (head + cols + "\n" + "\n".join(rows) + "\n").encode("utf-8")


@pytest.mark.parametrize("encoding", ["latin-1", "utf-8"])
def test_read_turbine_both_encodings(encoding):
    df = kelmarsh.read_turbine(turbine_csv(encoding))
    assert list(df["pitch_deg"]) == [1.5, 0.5, 0.0]          # degree-sign column found in both encodings
    assert str(df["time"].dt.tz) == "UTC" and len(df) == 3


def test_load_zip_window_and_wind_speed(tmp_path):
    path = tmp_path / "Kelmarsh_SCADA_2020_3086.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Turbine_Data_Kelmarsh_1_2020-01-01_-_2021-01-01_228.csv", turbine_csv("utf-8"))
        z.writestr("Status_Kelmarsh_1_2020-01-01_-_2021-01-01_228.csv", status_csv())
    assert kelmarsh.find_zips(tmp_path, [2020]) == [path]
    scada, events = kelmarsh.load([path], "2020-10-24 00:10", "2020-10-25")
    assert list(scada["wind_speed"]) == [6.0, 7.0]           # mean of both anemometers, NaN ignored
    assert list(events["status"]) == ["Stop"] and events["asset"].iloc[0] == "KWF1"


def test_stop_overlap_and_state():
    times = pd.Series(pd.date_range("2020-10-24", periods=4, freq="10min", tz="UTC"))
    stops = pd.DataFrame({"start": [pd.Timestamp("2020-10-24 00:05", tz="UTC")],
                          "end": [pd.Timestamp("2020-10-24 00:25", tz="UTC")]})
    assert list(kelmarsh.stop_overlap(times, stops)) == [300, 600, 300, 0]
    scada = pd.DataFrame({"time": times, "asset": "KWF1", "power_kw": [500.0, 0.0, 0.0, 10.0]})
    events = stops.assign(asset="KWF1", status="Stop", category="Forced outage")
    out = kelmarsh.add_state(scada, events)
    assert list(out["available"]) == [0.5, 0.0, 0.5, 1.0]
    assert list(out["state"]) == [4, 4, 4, 1]                # >= half the interval in a forced outage -> fault


def test_maintenance_beats_fault_and_open_stop():
    times = pd.Series(pd.date_range("2020-10-24", periods=2, freq="10min", tz="UTC"))
    events = pd.DataFrame({"start": [pd.Timestamp("2020-10-24", tz="UTC")],
                           "end": pd.Series([pd.NaT], dtype="datetime64[ns, UTC]"), "asset": "KWF2",
                           "status": "Stop", "category": "Scheduled Maintenance"})
    out = kelmarsh.add_state(pd.DataFrame({"time": times, "asset": "KWF2", "power_kw": [0.0, 900.0]}), events)
    assert list(out["state"]) == [3, 0] and list(out["available"]) == [0.0, 1.0]


def pvdaq_day(tmp_path, day="2016_06_04", ac=(-2.0, 500.0, 0.0), code=(0.0, 0.0, 139.0)):
    df = pd.DataFrame({"measured_on": [f"{day.replace('_', '-')} {t}" for t in ("05:00:00", "12:00:00", "12:15:00")],
                       "ac_power_hw__2695": ac, "dc_power_hw__2694": [0.0, 520.0, 0.0],
                       "poa_irradiance__2679": [10.0, 900.0, 950.0], "module_temp_f__2689": [0.0, 122.0, 140.0],
                       "ambient_temp_f__2688": [-40.0, 95.0, 96.8], "inverter_error_code__2690": code})
    df.to_csv(tmp_path / f"system_34__date_{day}.csv", index=False)


def test_pvdaq_units_time_and_state(tmp_path):
    pvdaq_day(tmp_path)
    df, events = pvdaq.load(tmp_path, "2016-06-04", "2016-06-05")
    assert list(df["time"].dt.hour) == [13, 20, 20]          # UTC-8 local standard time -> UTC
    assert list(df["ac_kw"]) == [0.0, 50.0, 0.0]             # hectowatt -> kW, tare clipped
    assert df["module_temp_c"].iloc[1] == pytest.approx(50.0) and np.isnan(df["module_temp_c"].iloc[0])
    assert np.isnan(df["ambient_temp_c"].iloc[0])            # -40 degF dropout
    assert list(df["state"]) == [1, 0, 4]
    assert np.isnan(df["available"].iloc[0]) and list(df["available"].iloc[1:]) == [1.0, 0.0]
    assert df["energy_kwh"].iloc[1] == pytest.approx(12.5)
    assert list(events["code"]) == [139] and events["asset"].iloc[0] == config.PV.asset


def test_pvdaq_missing_inverter_channels(tmp_path):
    pvdaq_day(tmp_path)
    pd.DataFrame({"measured_on": ["2016-06-05 12:00:00"], "poa_irradiance__2679": [900.0]}).to_csv(
        tmp_path / "system_34__date_2016_06_05.csv", index=False)
    df, _ = pvdaq.load(tmp_path, "2016-06-04", "2016-06-06")
    last = df.iloc[-1]
    assert np.isnan(last["ac_kw"]) and np.isnan(last["state"]) and np.isnan(last["available"])
    with pytest.raises(FileNotFoundError):
        pvdaq.load(tmp_path, "2016-06-04", "2016-06-08")


def test_despike_removes_single_glitches_only():
    x = pd.Series([30.0, 31.0, -20.0, 32.0, 33.0, 34.0, 20.0, 21.0, 22.0])
    out = pvdaq.despike(x)
    assert np.isnan(out[2]) and out.drop(2).notna().all()   # a real step change survives, the dip does not
