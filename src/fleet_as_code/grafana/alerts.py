"""Grafana-managed alert rules (file provisioning format), one instance per asset.

Windows and pending periods are defined in plant time and converted to wall time with the replay speed, see
config.SPEED. Each rule is query (A, Flux, one value per asset) -> reduce last (B) -> threshold (C).

Usage:
    from fleet_as_code.grafana.alerts import rules_file
    doc = rules_file(speed=60)          # dict, dumped to grafana/provisioning/alerting/fleet-rules.yaml
"""
from __future__ import annotations

from dataclasses import dataclass

from fleet_as_code import config
from fleet_as_code.grafana import flux

EVAL_INTERVAL_S = 10   # Grafana's minimum; also one wind record at 60x


@dataclass(frozen=True)
class Rule:
    uid: str
    title: str
    query: str
    window_h: float          # plant hours of data per evaluation
    op: str                  # "lt" or "gt"
    threshold: float
    for_h: float             # plant hours the condition must hold before firing
    severity: str
    summary: str
    description: str
    dashboard: str
    panel_title: str
    no_data: str = "OK"


def rules(speed: float = config.SPEED) -> list[Rule]:
    return [
        Rule("wind-underperformance", "Wind turbine underperformance", flux.performance_index(), 4.0, "lt", 0.85,
             0.5, "warning", "{{ $labels.asset }} delivers {{ if $values.B }}{{ humanizePercentage $values.B.Value }}{{ end }} of its reference power",
             "Actual / expected energy over the last 4 h of plant time against the turbine's own reference curve: "
             "fully available producing intervals expecting more than 500 kW, at least 2 h of them. Below 85 % "
             "points at derating, pitch or yaw misalignment, icing or a sensor fault.", "wind-turbine",
             "Performance index"),
        Rule("pv-underperformance", "PV underperformance", flux.performance_ratio(min_poa=300.0), 2.0, "lt", 0.35,
             0.5, "critical", "{{ $labels.asset }} performance ratio {{ if $values.B }}{{ humanizePercentage $values.B.Value }}{{ end }}",
             "Performance ratio over the last 2 h of plant time with POA above 300 W/m2 (at least 1 h of such "
             "records). The site normally runs at "
             "0.55-0.6; below 0.35 the inverter is off or heavily limited even if it reports no error.",
             "pv-plant", "Performance ratio"),
        Rule("no-data", "Asset data missing", flux.staleness(speed), 6.0, "gt", 30.0, 0.0, "critical",
             "No data from {{ $labels.asset }} for {{ if $values.B }}{{ humanize $values.B.Value }}{{ end }} min of plant time",
             "Minutes of plant time since the last power record. Fires when an asset is silent for more than "
             "30 min (3 wind / 2 PV records); when the whole replay stops the rule goes to NoData and alerts too.",
             "fleet-overview", "Power by asset", no_data="Alerting"),
        Rule("availability-drop", "Availability drop", flux.availability(), 24.0, "lt", 0.90, 0.0, "warning",
             "{{ $labels.asset }} availability {{ if $values.B }}{{ humanizePercentage $values.B.Value }}{{ end }} over the last 24 h",
             "Time-based availability over the last 24 h of plant time below 90 %: a long stop in an unavailable "
             "category (forced outage, maintenance) or, for PV, no production in daylight.",
             "fleet-overview", "Availability by asset"),
    ]


def _rule(r: Rule, speed: float, panel_ids: dict) -> dict:
    window = config.plant_to_wall(r.window_h * 3600, speed)
    pending = 0 if r.for_h == 0 else config.plant_to_wall(r.for_h * 3600, speed)
    annotations = {"summary": r.summary, "description": r.description,
                   "__dashboardUid__": r.dashboard}
    if (r.dashboard, r.panel_title) in panel_ids:
        annotations["__panelId__"] = str(panel_ids[(r.dashboard, r.panel_title)])
    return {
        "uid": r.uid, "title": r.title, "condition": "C",
        "data": [
            {"refId": "A", "relativeTimeRange": {"from": window, "to": 0}, "datasourceUid": config.DATASOURCE_UID,
             "model": {"refId": "A", "query": r.query, "intervalMs": 1000, "maxDataPoints": 43200}},
            {"refId": "B", "relativeTimeRange": {"from": 0, "to": 0}, "datasourceUid": "__expr__",
             "model": {"refId": "B", "type": "reduce", "expression": "A", "reducer": "last",
                       "settings": {"mode": "dropNN"}}},
            {"refId": "C", "relativeTimeRange": {"from": 0, "to": 0}, "datasourceUid": "__expr__",
             "model": {"refId": "C", "type": "threshold", "expression": "B",
                       "conditions": [{"evaluator": {"type": r.op, "params": [r.threshold]}}]}},
        ],
        "noDataState": r.no_data, "execErrState": "Error", "for": f"{pending}s",
        "annotations": annotations, "labels": {"severity": r.severity, "fleet": "demo"}, "isPaused": False,
    }


def rules_file(speed: float = config.SPEED, panel_ids: dict | None = None) -> dict:
    """panel_ids: {(dashboard uid, panel title): id} to link each rule to its panel."""
    return {"apiVersion": 1, "groups": [{
        "orgId": 1, "name": "fleet-health", "folder": config.ALERT_FOLDER_TITLE, "interval": f"{EVAL_INTERVAL_S}s",
        "rules": [_rule(r, speed, panel_ids or {}) for r in rules(speed)]}]}
