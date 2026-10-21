import pandas as pd
import pytest

from fleet_as_code import config
from fleet_as_code.replay import Fault, Source, build_lines, heartbeat, run

T0 = 1_800_000_000.0


def wind_source(start="2020-10-24 02:00"):
    times = pd.date_range("2020-10-24 00:00", periods=12, freq="10min", tz="UTC")
    data = pd.DataFrame({"time": times.repeat(2), "asset": ["KWF1", "KWF2"] * 12, "power_kw": 1000.0,
                         "expected_kw": 1000.0, "wind_speed": 9.0, "rotor_rpm": 14.0, "pitch_deg": 0.0,
                         "ambient_temp_c": 8.0, "energy_kwh": 166.7, "available": 1.0, "state": 0})
    events = pd.DataFrame({"start": [times[1], times[11]], "asset": ["KWF1", "KWF2"], "status": ["Stop", "Warning"],
                           "code": [3110, 100], "message": ["Frequency converter error", 'A "quoted" one'],
                           "category": ["Forced outage", "Full Performance"]})
    return Source.wind(data, events, start)


def test_fault_parse():
    assert Fault.parse("derate:KWF4:2:12:0.7") == Fault("derate", "KWF4", 2.0, 12.0, 0.7)
    assert Fault.parse("blackout:KWF2:6:3") == Fault("blackout", "KWF2", 6.0, 3.0, 1.0)
    with pytest.raises(ValueError):
        Fault.parse("explode:KWF1:1:1")


def test_time_mapping_backfill_and_duration():
    times, lines = build_lines([wind_source()], speed=60, t0=T0, backfill_h=1, duration_h=0.5)
    # plant 01:00 .. 02:30 kept -> 6 records x 2 turbines; of the events only the 01:50 warning is inside
    data = [l for l in lines if l.startswith("wind_turbine")]
    events = [l for l in lines if l.startswith("event")]
    assert len(data) == 12 and len(events) == 1 and "severity=Warning" in events[0]
    assert times[0] == pytest.approx(T0 - 3600 / 60) and times[-1] == pytest.approx(T0 + 1200 / 60)
    assert list(times) == sorted(times)
    assert data[0].endswith(f" {round((T0 - 60) * 1000)}")
    assert "state=0i" in data[0] and "site=kelmarsh" in data[0]


def test_events_are_written_with_tags():
    _, lines = build_lines([wind_source("2020-10-24 00:00")], speed=1, t0=T0, backfill_h=0, duration_h=2)
    events = [l for l in lines if l.startswith("event")]
    assert len(events) == 2
    assert events[0].startswith("event,asset=KWF1,category=Forced\\ outage,severity=Stop,site=kelmarsh code=3110i")
    assert 'message="A \\"quoted\\" one"' in events[1]


def test_faults_blackout_and_derate():
    faults = [Fault.parse("blackout:KWF2:0:1"), Fault.parse("derate:KWF1:0.5:10:0.5")]
    _, lines = build_lines([wind_source("2020-10-24 00:00")], speed=1, t0=T0, backfill_h=0, duration_h=2, faults=faults)
    kwf2 = [l for l in lines if l.startswith("wind_turbine,asset=KWF2")]
    assert len(kwf2) == 6                                   # first plant hour dropped
    kwf1 = [l for l in lines if l.startswith("wind_turbine,asset=KWF1")]
    assert "power_kw=1000" in kwf1[2] and "power_kw=500" in kwf1[3] and "energy_kwh=83.35" in kwf1[3]
    assert "expected_kw=1000" in kwf1[3]                    # the reference is not derated


def test_heartbeat_clock():
    hb = heartbeat([wind_source()], speed=60, t0=T0, now=T0 + 60)
    assert hb[0].startswith('replay,site=kelmarsh source_time_ms=') and 'plant_clock="2020-10-24 03:00 UTC"' in hb[0]
    pv = Source.pv(pd.DataFrame({"time": pd.to_datetime([], utc=True)}), pd.DataFrame(columns=["start"]),
                   "2016-06-04 14:00")
    assert 'plant_clock="2016-06-04 06:00 UTC-8"' in heartbeat([pv], 60, T0, T0)[0]


class FakeDb:
    def __init__(self):
        self.batches = []

    def write(self, bucket, lines):
        assert bucket == config.BUCKET
        self.batches.append(lines)


def test_run_writes_each_line_once_on_a_fake_clock():
    src = wind_source()
    times, lines = build_lines([src], speed=600, t0=T0, backfill_h=1, duration_h=0.5)
    now = [T0]
    db = FakeDb()
    written = run(db, times, lines, [src], speed=600, t0=T0, clock=lambda: now[0],
                  sleep=lambda s: now.__setitem__(0, now[0] + max(s, 0.5)))
    data = [l for b in db.batches for l in b if not l.startswith("replay")]
    assert written == len(lines) == len(data) and sorted(data) == sorted(lines)
    assert len(db.batches[0]) > 1 and all(b[-1].startswith("replay") for b in db.batches)
