"""The four dashboards: fleet overview, wind turbine detail, PV plant and alarms.

Usage:
    from fleet_as_code.grafana.dashboards import build_all
    for model in build_all(speed=60):
        print(model["uid"], len(model["panels"]))
"""
from __future__ import annotations

from fleet_as_code import config
from fleet_as_code.grafana import flux
from fleet_as_code.grafana.panels import (Grid, alertlist, barchart, bargauge, dashboard, query_variable,
                                          state_mappings, stat, state_timeline, table, text, timeseries, xychart)

AVAILABILITY_STEPS = [(None, "red"), (0.9, "orange"), (0.97, "green")]
PERFORMANCE_STEPS = [(None, "red"), (0.85, "orange"), (0.95, "green")]
PR_STEPS = [(None, "red"), (0.35, "orange"), (0.5, "green")]
ASSET = "${__field.labels.asset}"


def _rename(name: str, display: str) -> dict:
    return {"matcher": {"id": "byName", "options": name}, "properties": [{"id": "displayName", "value": display}]}


def _dashed(name: str, color: str) -> dict:
    return {"matcher": {"id": "byName", "options": name}, "properties": [
        {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}},
        {"id": "custom.fillOpacity", "value": 0}, {"id": "color", "value": {"mode": "fixed", "fixedColor": color}}]}


def _right_axis(name: str, unit: str, color: str) -> dict:
    return {"matcher": {"id": "byName", "options": name}, "properties": [
        {"id": "custom.axisPlacement", "value": "right"}, {"id": "unit", "value": unit},
        {"id": "custom.fillOpacity", "value": 0}, {"id": "color", "value": {"mode": "fixed", "fixedColor": color}}]}


def _width(name: str, px: int) -> dict:
    return {"matcher": {"id": "byName", "options": name}, "properties": [{"id": "custom.width", "value": px}]}


def _speed_note(speed: float) -> str:
    if speed == 1:
        return "Real-time replay."
    return (f"Replay at {speed:g}x: one minute on the time axis is {speed:g} minutes of plant time, so the default "
            f"'last 1 hour' shows {speed / 24:g} days of operation.")


def fleet_overview(speed: float) -> dict:
    g = Grid()
    panels = [
        stat("Plant clock", flux.plant_clock(), "string", g.next(6, 4), display_name="${__field.labels.site}",
             fields="/.*/", text_size=16,
             description="Historical time being replayed at each site. " + _speed_note(speed)),
        stat("Fleet power", flux.fleet_power_now(), "kwatt", g.next(3, 4), decimals=1,
             description="Latest power of all turbines and the PV inverter."),
        stat("Energy in range", flux.energy_by_source(), "kwatth", g.next(5, 4), decimals=1,
             display_name="${__field.labels.source}",
             description="Exported energy in the selected time range (sum of 10/15-minute energy values)."),
        stat("Wind availability", flux.wind_availability(), "percentunit", g.next(4, 4), decimals=1,
             steps=AVAILABILITY_STEPS, color_mode="background",
             description="Time-based availability (IEC 61400-26-1 categories) across all turbines."),
        stat("Wind capacity factor", flux.wind_capacity_factor(), "percentunit", g.next(3, 4), decimals=1,
             description="Mean power / rated power over the range."),
        stat("PV performance ratio", flux.performance_ratio(), "percentunit", g.next(3, 4), decimals=1,
             steps=PR_STEPS, color_mode="background",
             description="IEC 61724-1 PR over records with POA above 50 W/m2. This roof normally runs at PR 0.55-0.6."),
        timeseries("Power by asset", flux.power_by_asset(), "kwatt", g.next(16, 9), display_name=ASSET, stack=True,
                   fill=60, interval=f"{config.plant_to_wall(1200, speed)}s", min_=0),
        alertlist("Active alerts", g.next(8, 9), instances=False),
        table("Assets", [flux.asset_table(), flux.asset_performance()], g.next(14, 9),
              transformations=[{"id": "joinByField", "options": {"byField": "asset", "mode": "outer"}},
                               {"id": "organize", "options": {
                                   "excludeByName": {}, "indexByName": {"asset": 0, "_measurement": 1, "state": 2,
                                                                        "energy_kwh": 3, "available": 4,
                                                                        "performance": 5},
                                   "renameByName": {"asset": "Asset", "_measurement": "Type", "state": "State",
                                                    "energy_kwh": "Energy", "available": "Availability",
                                                    "performance": "Performance"}}}],
              overrides=[
                  _width("Asset", 80), _width("Type", 120), _width("State", 170),
                  {"matcher": {"id": "byName", "options": "Energy"},
                   "properties": [{"id": "unit", "value": "kwatth"}, {"id": "decimals", "value": 1}]},
                  {"matcher": {"id": "byName", "options": "Availability"},
                   "properties": [{"id": "unit", "value": "percentunit"}, {"id": "decimals", "value": 1},
                                  {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                                  {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                                      {"value": None, "color": "red"}, {"value": 0.9, "color": "orange"},
                                      {"value": 0.97, "color": "green"}]}}]},
                  {"matcher": {"id": "byName", "options": "Performance"},
                   "properties": [{"id": "unit", "value": "percentunit"}, {"id": "decimals", "value": 1}]},
                  {"matcher": {"id": "byName", "options": "State"},
                   "properties": [{"id": "mappings", "value": state_mappings()},
                                  {"id": "custom.cellOptions", "value": {"type": "color-background"}}]},
                  {"matcher": {"id": "byName", "options": "Type"},
                   "properties": [{"id": "mappings", "value": [{"type": "value", "options": {
                       "wind_turbine": {"text": "Wind turbine", "index": 0},
                       "pv_inverter": {"text": "PV inverter", "index": 1}}}]}]},
                  {"matcher": {"id": "byName", "options": "Asset"},
                   "properties": [{"id": "links", "value": [
                       {"title": "Turbine detail", "url": "/d/wind-turbine?var-turbine=${__value.raw}&${__url_time_range}"}]}]},
              ],
              description="Energy, availability and last state per asset. Performance is actual / expected energy "
                          "for turbines and the performance ratio for PV. Click a turbine for its detail dashboard."),
        bargauge("Availability by asset", flux.availability(), "percentunit", g.next(10, 9),
                 steps=AVAILABILITY_STEPS),
        state_timeline("Asset state", flux.asset_states(), g.next(24, 7), interval=f"{config.plant_to_wall(600, speed)}s",
                       description="Wind: from the turbine status log. PV: inverter error code and production."),
    ]
    return dashboard("fleet-overview", "Fleet overview", panels,
                     description="Wind and PV fleet KPIs from the live replay. " + _speed_note(speed))


def wind_turbine(speed: float) -> dict:
    g = Grid()
    t = flux.TURBINE
    panels = [
        stat("Power", flux.turbine_last("power_kw"), "kwatt", g.next(4, 4)),
        stat("Wind speed", flux.turbine_last("wind_speed"), "velocityms", g.next(4, 4), decimals=1),
        stat("Energy in range", flux.turbine_sum("energy_kwh"), "kwatth", g.next(4, 4), decimals=1),
        stat("Availability", flux.turbine_mean("available"), "percentunit", g.next(4, 4), decimals=1,
             steps=AVAILABILITY_STEPS, color_mode="background"),
        stat("Performance index", flux.performance_index(t), "percentunit", g.next(4, 4), decimals=1,
             steps=PERFORMANCE_STEPS, color_mode="background",
             description="Actual / expected energy over fully available producing intervals expecting more than "
                         "500 kW; expected power comes from the turbine's reference curve at the measured wind speed."),
        stat("Capacity factor", flux.turbine_mean("power_kw", config.TURBINES[0].rated_kw), "percentunit",
             g.next(4, 4), decimals=1),
        xychart("Power curve", [flux.turbine_scatter(), flux.reference_curve()], g.next(12, 12),
                [{"name": "10-min data", "x": "wind_speed", "y": "power_kw"},
                 {"name": "Reference curve", "x": "wind_speed", "y": "power_kw"}],
                x_unit="velocityms", y_unit="kwatt", x_label="Wind speed (mean of both anemometers)",
                y_label="Power",
                overrides=[{"matcher": {"id": "byFrameRefID", "options": "B"}, "properties": [
                    {"id": "custom.show", "value": "lines"},
                    {"id": "color", "value": {"mode": "fixed", "fixedColor": "orange"}}]}],
                description="10-minute means in the time range against the turbine's own reference curve (method "
                            "of bins on its 2019 data)."),
        timeseries("Power vs expected", [flux.turbine_series(["power_kw", "expected_kw"]),
                                         flux.turbine_series(["wind_speed"])], "kwatt", g.next(12, 12),
                   overrides=[_rename("power_kw", "Power"), _rename("expected_kw", "Expected"),
                              _dashed("expected_kw", "orange"), _rename("wind_speed", "Wind speed"),
                              _right_axis("wind_speed", "velocityms", "blue")], min_=0),
        state_timeline("State", flux.turbine_states(), g.next(24, 4), display_name=t,
                       interval=f"{config.plant_to_wall(600, speed)}s"),
        timeseries("Rotor speed and pitch", flux.turbine_series(["rotor_rpm", "pitch_deg"]), "rotrpm",
                   g.next(12, 8), fill=0, overrides=[_rename("rotor_rpm", "Rotor speed"), _rename("pitch_deg", "Pitch"),
                                                     _right_axis("pitch_deg", "degree", "purple")]),
        table("Status events", flux.events(t), g.next(12, 8), sort_by="Time", no_value="No events in range",
              transformations=[{"id": "organize", "options": {"renameByName": {
                  "_time": "Time", "severity": "Severity", "category": "IEC category", "code": "Code",
                  "message": "Message", "asset": "Asset"}, "excludeByName": {"asset": True}}}]),
    ]
    variables = [query_variable("turbine", "Turbine", flux.turbine_variable(), config.TURBINES[0].asset)]
    return dashboard("wind-turbine", "Wind turbine", panels, variables=variables,
                     description="One Kelmarsh turbine: power curve, performance, state and status log. "
                                 + _speed_note(speed))


def pv_plant(speed: float) -> dict:
    g = Grid()
    hour = f"{config.plant_to_wall(3600, speed)}s"
    panels = [
        stat("AC power", flux.pv_last("ac_kw"), "kwatt", g.next(4, 4)),
        stat("Irradiance (POA)", flux.pv_last("poa_wm2"), "Wm2", g.next(4, 4), decimals=0),
        stat("Energy in range", flux.pv_energy(), "kwatth", g.next(4, 4), decimals=1),
        stat("Irradiation in range", flux.pv_irradiation(), "suffix: kWh/m²", g.next(4, 4), decimals=2,
             description="Plane-of-array irradiation in kWh/m2."),
        stat("Performance ratio", flux.performance_ratio(), "percentunit", g.next(4, 4), decimals=1,
             steps=PR_STEPS, color_mode="background"),
        stat("Availability", flux.pv_availability(), "percentunit", g.next(4, 4), decimals=1,
             steps=AVAILABILITY_STEPS, color_mode="background",
             description="Share of daylight records (POA > 50 W/m2) with the inverter producing (IEC 61724-3)."),
        timeseries("AC power and irradiance", [flux.pv_series(["ac_kw"]), flux.pv_series(["poa_wm2"])], "kwatt",
                   g.next(16, 9), overrides=[_rename("ac_kw", "AC power"), _rename("poa_wm2", "POA irradiance"),
                                             _right_axis("poa_wm2", "Wm2", "yellow")], min_=0),
        xychart("Irradiance vs power", [flux.pv_scatter()], g.next(8, 9),
                [{"name": "15-min data", "x": "poa_wm2", "y": "ac_kw"}], x_unit="Wm2", y_unit="kwatt",
                x_label="Plane-of-array irradiance", y_label="AC power",
                description="A healthy inverter follows a straight line; a failed one sits on the x axis."),
        timeseries("Performance ratio per plant hour", flux.pv_pr_series(hour), "percentunit", g.next(12, 8),
                   draw="points", fill=0, steps=[(None, "red"), (0.35, "transparent")], threshold_style="area",
                   display_name="Hourly PR", min_=0,
                   description="Hourly PR (window means, POA > 200 W/m2). The red band is the alert threshold."),
        timeseries("Temperatures", flux.pv_series(["module_temp_c", "ambient_temp_c"]), "celsius", g.next(12, 8),
                   fill=0, overrides=[_rename("module_temp_c", "Module"), _rename("ambient_temp_c", "Ambient")]),
        state_timeline("Inverter state", flux.pv_states(), g.next(12, 5), interval=f"{config.plant_to_wall(900, speed)}s"),
        table("Inverter events", flux.events(config.PV.asset), g.next(12, 5), sort_by="Time",
              no_value="No inverter error codes in range",
              transformations=[{"id": "organize", "options": {"renameByName": {
                  "_time": "Time", "severity": "Severity", "category": "Category", "code": "Code",
                  "message": "Message"}, "excludeByName": {"asset": True}}}]),
    ]
    return dashboard("pv-plant", "PV plant", panels,
                     description="NREL PVDAQ system 34: 146.6 kWp roof, one 135 kW inverter. " + _speed_note(speed))


def alarms(speed: float) -> dict:
    g = Grid()
    panels = [
        alertlist("Alerts", g.next(10, 10), description="Firing and pending alert instances (label fleet=demo)."),
        barchart("Events by IEC category", flux.event_counts("category"), g.next(7, 10), x_field="category"),
        barchart("Events by asset", flux.event_counts("asset"), g.next(7, 10), x_field="asset", color="red"),
        state_timeline("Asset state", flux.asset_states(), g.next(24, 7), interval=f"{config.plant_to_wall(600, speed)}s"),
        table("Event log", flux.events(), g.next(24, 10), sort_by="Time", no_value="No events in range",
              transformations=[{"id": "organize", "options": {"renameByName": {
                  "_time": "Time", "asset": "Asset", "severity": "Severity", "category": "IEC category",
                  "code": "Code", "message": "Message"}, "indexByName": {"_time": 0, "asset": 1, "severity": 2,
                                                                         "category": 3, "code": 4, "message": 5}}}],
              overrides=[{"matcher": {"id": "byName", "options": "Severity"}, "properties": [
                  {"id": "mappings", "value": [{"type": "value", "options": {
                      "Stop": {"color": "red", "index": 0}, "Warning": {"color": "orange", "index": 1}}}]},
                  {"id": "custom.cellOptions", "value": {"type": "color-text"}}]}],
              description="Stop and warning events from the turbine status logs and inverter error codes."),
    ]
    return dashboard("alarms", "Alarms", panels, description="Alert state and the event log. " + _speed_note(speed))


def build_all(speed: float = config.SPEED) -> list[dict]:
    return [fleet_overview(speed), wind_turbine(speed), pv_plant(speed), alarms(speed)]
