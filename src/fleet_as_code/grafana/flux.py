"""Flux queries used by the dashboards and alert rules.

Dashboard queries read the panel time range through Grafana's v.timeRangeStart / v.timeRangeStop and
v.windowPeriod; alert rules use the same variables, filled from each rule's relative time range. Every query only
references measurements and fields from config.MEASUREMENTS (checked by the tests).

Usage:
    from fleet_as_code.grafana import flux
    print(flux.fleet_power_now())
"""
from __future__ import annotations

from fleet_as_code import config

RANGE = "|> range(start: v.timeRangeStart, stop: v.timeRangeStop)"
WIND, PV, EVENT = "wind_turbine", "pv_inverter", "event"
TURBINE = "${turbine}"
# Aggregates lose _time; Grafana alerting wants one numeric value per series with a time column and plain labels.
STAMP = ('  |> map(fn: (r) => ({r with _time: r._stop}))\n'
         '  |> keep(columns: ["_time", "_value", "site", "asset"])')
# The state timeline applies value mappings (code -> text, colour) to string values only.
AS_TEXT = "  |> map(fn: (r) => ({r with _value: string(v: r._value)}))"


def _fields(fields) -> str:
    return " or ".join(f'r._field == "{f}"' for f in fields)


def source(measurement: str, fields, asset: str | None = None, bucket: str = config.BUCKET) -> str:
    """from() |> range() |> filter() for one measurement, some fields and optionally one asset."""
    cond = f'r._measurement == "{measurement}"'
    if fields:
        cond += f" and ({_fields(fields)})" if len(fields) > 1 else f' and r._field == "{fields[0]}"'
    if asset:
        cond += f' and r.asset == "{asset}"'
    return f'from(bucket: "{bucket}")\n  {RANGE}\n  |> filter(fn: (r) => {cond})'


def fleet_power_source() -> str:
    return (f'from(bucket: "{config.BUCKET}")\n  {RANGE}\n'
            f'  |> filter(fn: (r) => (r._measurement == "{WIND}" and r._field == "power_kw") or '
            f'(r._measurement == "{PV}" and r._field == "ac_kw"))')


# --- shared building blocks --------------------------------------------------------------------------------------

def performance_index(asset: str | None = None, min_expected: float = 500.0, min_records: int = 12) -> str:
    """Wind: sum(actual) / sum(expected) per asset over fully available intervals where the turbine is producing
    (state 0) and expects more than min_expected kW (below that the steep part of the curve turns small wind speed
    errors into large relative ones); at least min_records of them (12 = 2 h). Stops, and restart attempts inside
    a stop, are left to the availability rule, so this isolates derating."""
    return (source(WIND, ["power_kw", "expected_kw", "state", "available"], asset) + "\n"
            '  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n'
            "  |> filter(fn: (r) => exists r.power_kw and exists r.expected_kw and exists r.state and exists r.available)\n"
            f"  |> filter(fn: (r) => r.state == 0 and r.available >= 1.0 and r.expected_kw > {min_expected:.1f})\n"
            "  |> reduce(identity: {actual: 0.0, expected: 0.0, n: 0}, fn: (r, accumulator) => "
            "({actual: accumulator.actual + r.power_kw, expected: accumulator.expected + r.expected_kw, "
            "n: accumulator.n + 1}))\n"
            f"  |> filter(fn: (r) => r.n >= {min_records} and r.expected > 0.0)\n"
            "  |> map(fn: (r) => ({r with _value: r.actual / r.expected}))\n" + STAMP)


def performance_ratio(min_poa: float = config.PV_DAYLIGHT_WM2, min_records: int = 4) -> str:
    """PV: IEC 61724-1 PR = E_AC / (P_STC * H_POA / 1000 W/m2), over records with POA above min_poa; at least
    min_records of them (4 = 1 h)."""
    return (source(PV, ["ac_kw", "poa_wm2"]) + "\n"
            '  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n'
            f"  |> filter(fn: (r) => exists r.ac_kw and exists r.poa_wm2 and r.poa_wm2 > {min_poa:.1f})\n"
            "  |> reduce(identity: {e: 0.0, h: 0.0, n: 0}, fn: (r, accumulator) => "
            f"({{e: accumulator.e + r.ac_kw, h: accumulator.h + r.poa_wm2 / 1000.0 * {config.PV.pdc_kwp}, "
            "n: accumulator.n + 1}))\n"
            f"  |> filter(fn: (r) => r.n >= {min_records} and r.h > 0.0)\n"
            "  |> map(fn: (r) => ({r with _value: r.e / r.h}))\n" + STAMP)


def staleness(speed: float) -> str:
    """Minutes of plant time since each asset's last record (wall-clock age multiplied by the replay speed)."""
    return (fleet_power_source() + "\n"
            "  |> last()\n"
            f"  |> map(fn: (r) => ({{r with _value: float(v: int(v: now()) - int(v: r._time)) / 1000000000.0 * {float(speed)} / 60.0}}))\n"
            '  |> keep(columns: ["_time", "_value", "site", "asset"])')


def availability() -> str:
    """Mean time-based availability per asset (wind: 10-min fractions, PV: daylight records)."""
    return (f'from(bucket: "{config.BUCKET}")\n  {RANGE}\n'
            f'  |> filter(fn: (r) => (r._measurement == "{WIND}" or r._measurement == "{PV}") and r._field == "available")\n'
            "  |> mean()\n" + STAMP)


# --- fleet overview ---------------------------------------------------------------------------------------------

def plant_clock() -> str:
    return source("replay", ["plant_clock"]) + '\n  |> last()\n  |> keep(columns: ["_value", "_field", "site"])'


def fleet_power_now() -> str:
    return fleet_power_source() + "\n  |> last()\n  |> group()\n  |> sum()"


def energy_by_source() -> str:
    parts = [f"{label.lower()} = " + source(measurement, ["energy_kwh"]) +
             f'\n  |> group()\n  |> sum()\n  |> set(key: "source", value: "{label}")\n  |> group(columns: ["source"])'
             for label, measurement in (("Wind", WIND), ("PV", PV))]
    return "\n".join(parts) + "\nunion(tables: [wind, pv])"


def wind_availability() -> str:
    return source(WIND, ["available"]) + "\n  |> group()\n  |> mean()"


def wind_capacity_factor() -> str:
    rated = sum(t.rated_kw for t in config.TURBINES) / len(config.TURBINES)
    return (source(WIND, ["power_kw"]) + "\n  |> group()\n  |> mean()\n"
            f"  |> map(fn: (r) => ({{r with _value: r._value / {rated}}}))")


def power_by_asset() -> str:
    return fleet_power_source() + "\n  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)"


def asset_table() -> str:
    data = (f'from(bucket: "{config.BUCKET}")\n  {RANGE}\n'
            f'  |> filter(fn: (r) => r._measurement == "{WIND}" or r._measurement == "{PV}")')
    return (f"data = {data}\n"
            'energy = data |> filter(fn: (r) => r._field == "energy_kwh") |> sum()\n'
            'avail = data |> filter(fn: (r) => r._field == "available") |> mean()\n'
            'state = data |> filter(fn: (r) => r._field == "state") |> last() |> toFloat()\n'
            "union(tables: [energy, avail, state])\n"
            '  |> group()\n'
            '  |> pivot(rowKey: ["asset", "_measurement"], columnKey: ["_field"], valueColumn: "_value")\n'
            '  |> keep(columns: ["asset", "_measurement", "energy_kwh", "available", "state"])')


def asset_performance() -> str:
    return (f"wind = {performance_index()}\n"
            f"pv = {performance_ratio()}\n"
            "union(tables: [wind, pv])\n"
            "  |> group()\n"
            '  |> keep(columns: ["asset", "_value"])\n'
            '  |> rename(columns: {_value: "performance"})')


def asset_states() -> str:
    return (f'from(bucket: "{config.BUCKET}")\n  {RANGE}\n'
            f'  |> filter(fn: (r) => (r._measurement == "{WIND}" or r._measurement == "{PV}") and r._field == "state")\n'
            "  |> aggregateWindow(every: v.windowPeriod, fn: last, createEmpty: false)\n" + AS_TEXT)


def wind_speed_mean() -> str:
    return (source(WIND, ["wind_speed"]) + "\n  |> group()\n"
            "  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)\n"
            '  |> set(key: "_field", value: "fleet mean wind speed")')


# --- turbine detail ----------------------------------------------------------------------------------------------

def turbine_last(field: str) -> str:
    return source(WIND, [field], TURBINE) + "\n  |> last()"


def turbine_sum(field: str) -> str:
    return source(WIND, [field], TURBINE) + "\n  |> sum()"


def turbine_mean(field: str, scale: float = 1.0) -> str:
    q = source(WIND, [field], TURBINE) + "\n  |> mean()"
    return q + (f"\n  |> map(fn: (r) => ({{r with _value: r._value / {scale}}}))" if scale != 1.0 else "")


def turbine_series(fields) -> str:
    return source(WIND, fields, TURBINE) + "\n  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)"


def turbine_scatter() -> str:
    return (source(WIND, ["wind_speed", "power_kw"], TURBINE) + "\n"
            '  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n'
            "  |> group()\n"
            '  |> keep(columns: ["wind_speed", "power_kw"])')


def reference_curve() -> str:
    return (f'from(bucket: "{config.REFERENCE_BUCKET}")\n'
            "  |> range(start: 0, stop: 1000000)\n"
            f'  |> filter(fn: (r) => r._measurement == "power_curve" and r.asset == "{TURBINE}")\n'
            '  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n'
            "  |> group()\n"
            '  |> keep(columns: ["wind_speed", "power_kw"])')


def turbine_states() -> str:
    return (source(WIND, ["state"], TURBINE) + "\n  |> aggregateWindow(every: v.windowPeriod, fn: last, createEmpty: false)\n"
            + AS_TEXT)


def events(asset: str | None = None, limit: int = 200) -> str:
    return (source(EVENT, [], asset) + "\n"
            '  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n'
            "  |> group()\n"
            '  |> keep(columns: ["_time", "asset", "severity", "category", "code", "message"])\n'
            '  |> sort(columns: ["_time"], desc: true)\n'
            f"  |> limit(n: {limit})")


def turbine_variable() -> str:
    return ('import "influxdata/influxdb/schema"\n'
            f'schema.tagValues(bucket: "{config.BUCKET}", tag: "asset", '
            f'predicate: (r) => r._measurement == "{WIND}", start: -30d)')


# --- PV plant -----------------------------------------------------------------------------------------------------

def pv_last(field: str) -> str:
    return source(PV, [field]) + "\n  |> last()"


def pv_energy() -> str:
    return source(PV, ["energy_kwh"]) + "\n  |> sum()"


def pv_irradiation() -> str:
    """kWh/m2 in the plane of array: 15-min means times 0.25 h."""
    return source(PV, ["poa_wm2"]) + "\n  |> sum()\n  |> map(fn: (r) => ({r with _value: r._value * 0.25 / 1000.0}))"


def pv_availability() -> str:
    return source(PV, ["available"]) + "\n  |> mean()"


def pv_series(fields) -> str:
    return source(PV, fields) + "\n  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)"


def pv_scatter() -> str:
    return (source(PV, ["poa_wm2", "ac_kw"]) + "\n"
            '  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n'
            "  |> filter(fn: (r) => exists r.poa_wm2 and exists r.ac_kw)\n"
            "  |> group()\n"
            '  |> keep(columns: ["poa_wm2", "ac_kw"])')


def pv_pr_series(every: str, min_poa: float = 200.0) -> str:
    """PR per window of plant time (window means of AC power and POA), skipping low-irradiance windows."""
    return (source(PV, ["ac_kw", "poa_wm2"]) + "\n"
            f"  |> aggregateWindow(every: {every}, fn: mean, createEmpty: false)\n"
            '  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n'
            f"  |> filter(fn: (r) => exists r.ac_kw and exists r.poa_wm2 and r.poa_wm2 > {min_poa:.1f})\n"
            f'  |> map(fn: (r) => ({{_time: r._time, _field: "PR", _value: r.ac_kw / (r.poa_wm2 / 1000.0 * {config.PV.pdc_kwp})}}))')


def pv_states() -> str:
    return source(PV, ["state"]) + "\n  |> aggregateWindow(every: v.windowPeriod, fn: last, createEmpty: false)\n" + AS_TEXT


# --- alarms -------------------------------------------------------------------------------------------------------

def event_counts(by: str) -> str:
    return (source(EVENT, ["code"]) + "\n"
            f'  |> group(columns: ["{by}"])\n'
            "  |> count()\n"
            "  |> group()\n"
            f'  |> keep(columns: ["{by}", "_value"])\n'
            '  |> rename(columns: {_value: "events"})\n'
            '  |> sort(columns: ["events"], desc: true)')
