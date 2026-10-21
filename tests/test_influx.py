import math
import os

import numpy as np

from fleet_as_code.influx import line, load_env


def test_line_types_and_order():
    lp = line("wind_turbine", {"site": "kelmarsh", "asset": "KWF1"},
              {"power_kw": 812.5, "state": np.int64(0), "message": 'say "hi"', "ok": True}, ts_ms=1700000000000)
    assert lp == ('wind_turbine,asset=KWF1,site=kelmarsh power_kw=812.5,state=0i,message="say \\"hi\\"",ok=true '
                  "1700000000000")


def test_line_escaping():
    lp = line("my meas", {"category": "Forced outage", "k,x": "a=b"}, {"field one": 1.0})
    assert lp == r"my\ meas,category=Forced\ outage,k\,x=a\=b field\ one=1"


def test_nan_fields_dropped_and_empty_line_is_none():
    assert line("m", {"a": "b"}, {"x": math.nan, "y": 2.0}) == "m,a=b y=2"
    assert line("m", {"a": "b"}, {"x": float("nan"), "y": None}) is None


def test_empty_tags_skipped_and_float_precision():
    assert line("m", {"a": "", "b": None, "c": "1"}, {"v": 1 / 3}) == "m,c=1 v=0.3333333"
    assert line("m", {}, {"v": 1e7}) == "m v=1e+07"


def test_load_env_does_not_override(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nFLEET_TEST_A=from-file\nFLEET_TEST_B = 'quoted'\nnot a pair\n", encoding="utf-8")
    monkeypatch.setenv("FLEET_TEST_A", "from-env")
    monkeypatch.delenv("FLEET_TEST_B", raising=False)
    load_env(env)
    assert os.environ["FLEET_TEST_A"] == "from-env" and os.environ["FLEET_TEST_B"] == "quoted"
