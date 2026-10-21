"""Builders for the Grafana dashboard JSON model (classic schema, as served by /api/dashboards/uid).

Plain dicts on purpose: every key Grafana stores is visible here and in the generated JSON, and nothing breaks
when Grafana adds a panel option. Panels are placed with Grid, ids are assigned by dashboard().

Usage:
    g = Grid()
    panels = [stat("Fleet power", flux, "kwatt", g.next(6, 4)), timeseries("Power", flux2, "kwatt", g.next(24, 8))]
    model = dashboard("fleet-overview", "Fleet overview", panels)
"""
from __future__ import annotations

from fleet_as_code import config

DATASOURCE = {"type": "influxdb", "uid": config.DATASOURCE_UID}
SCHEMA_VERSION = 41


class Grid:
    """Left-to-right, top-to-bottom placement on Grafana's 24-column grid."""

    def __init__(self):
        self.x = self.y = self.row_h = 0

    def next(self, w: int, h: int) -> dict:
        if self.x + w > 24:
            self.x, self.y, self.row_h = 0, self.y + self.row_h, 0
        pos = {"x": self.x, "y": self.y, "w": w, "h": h}
        self.x += w
        self.row_h = max(self.row_h, h)
        return pos

    def newline(self) -> None:
        if self.x:
            self.x, self.y, self.row_h = 0, self.y + self.row_h, 0


def target(query: str, ref: str = "A") -> dict:
    return {"refId": ref, "datasource": DATASOURCE, "query": query}


def thresholds(*steps: tuple[float | None, str]) -> dict:
    """thresholds((None, "red"), (0.9, "orange"), (0.97, "green"))"""
    return {"mode": "absolute", "steps": [{"value": v, "color": c} for v, c in steps]}


def state_mappings() -> list[dict]:
    return [{"type": "value", "options": {str(code): {"text": text, "color": color, "index": i}
                                          for i, (code, (text, color)) in enumerate(config.STATES.items())}}]


def _panel(kind: str, title: str, grid: dict, queries: list[str] | str | None, *, description: str = "",
           defaults: dict | None = None, overrides: list | None = None, options: dict | None = None, **extra) -> dict:
    if isinstance(queries, str):
        queries = [queries]
    # without pluginVersion Grafana treats the options as written for an old panel version and migrates them
    # (the XY chart, for one, then drops its manual series mapping)
    p = {"type": kind, "title": title, "gridPos": grid, "pluginVersion": config.GRAFANA_VERSION}
    if description:
        p["description"] = description
    if queries:
        p["datasource"] = DATASOURCE
        p["targets"] = [target(q, chr(ord("A") + i)) for i, q in enumerate(queries)]
    p["fieldConfig"] = {"defaults": defaults or {}, "overrides": overrides or []}
    p["options"] = options or {}
    p.update(extra)
    return p


def stat(title: str, query: str, unit: str, grid: dict, *, decimals: int | None = None, steps=None,
         display_name: str | None = None, color_mode: str = "value", calc: str = "lastNotNull",
         fields: str = "", description: str = "", mappings: list | None = None, text_size: int | None = None) -> dict:
    defaults = {"unit": unit, "color": {"mode": "thresholds"},
                "thresholds": thresholds(*(steps or [(None, "text")])), "mappings": mappings or []}
    if decimals is not None:
        defaults["decimals"] = decimals
    if display_name:
        defaults["displayName"] = display_name
    options = {"reduceOptions": {"calcs": [calc], "fields": fields, "values": False}, "orientation": "auto",
               "textMode": "value_and_name" if display_name else "auto", "colorMode": color_mode,
               "graphMode": "none", "justifyMode": "auto", "wideLayout": True, "showPercentChange": False}
    if text_size:
        options["text"] = {"valueSize": text_size}
    return _panel("stat", title, grid, query, description=description, defaults=defaults, options=options)


def timeseries(title: str, queries: list[str] | str, unit: str, grid: dict, *, display_name: str | None = None,
               stack: bool = False, fill: int = 10, overrides: list | None = None, steps=None,
               threshold_style: str = "off", interval: str | None = None, description: str = "",
               draw: str = "line", axis_label: str = "", min_: float | None = None) -> dict:
    custom = {"drawStyle": draw, "lineWidth": 1, "fillOpacity": fill, "gradientMode": "opacity" if fill else "none",
              "showPoints": "never", "spanNulls": False, "axisPlacement": "auto", "axisLabel": axis_label,
              "stacking": {"mode": "normal" if stack else "none", "group": "A"},
              "thresholdsStyle": {"mode": threshold_style}}
    defaults = {"unit": unit, "custom": custom, "color": {"mode": "palette-classic"},
                "thresholds": thresholds(*(steps or [(None, "green")]))}
    if display_name:
        defaults["displayName"] = display_name
    if min_ is not None:
        defaults["min"] = min_
    options = {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True, "calcs": []},
               "tooltip": {"mode": "multi", "sort": "desc"}}
    extra = {"interval": interval} if interval else {}
    return _panel("timeseries", title, grid, queries, description=description, defaults=defaults,
                  overrides=overrides, options=options, **extra)


def xychart(title: str, queries: list[str], grid: dict, series: list[dict], *, x_unit: str, y_unit: str,
            x_label: str = "", y_label: str = "", overrides: list | None = None, description: str = "") -> dict:
    """series: [{"name": "10-min data", "x": "wind_speed", "y": "power_kw"}, ...], one per query (frame)."""
    mapped = [{"frame": {"matcher": {"id": "byIndex", "options": i}},
               "x": {"matcher": {"id": "byName", "options": s["x"]}},
               "y": {"matcher": {"id": "byName", "options": s["y"]}},
               "name": {"fixed": s["name"]}} for i, s in enumerate(series)]
    defaults = {"custom": {"show": "points", "pointSize": {"fixed": 3}, "pointShape": "circle",
                           "pointStrokeWidth": 1, "fillOpacity": 40, "lineWidth": 2, "lineStyle": {"fill": "solid"},
                           "axisPlacement": "auto", "axisLabel": y_label},
                "color": {"mode": "palette-classic"}, "unit": y_unit}
    x_override = {"matcher": {"id": "byName", "options": series[0]["x"]}, "properties": [{"id": "unit", "value": x_unit},
                                                                              {"id": "custom.axisLabel", "value": x_label}]}
    options = {"mapping": "manual", "series": mapped,
               "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True, "calcs": []},
               "tooltip": {"mode": "single", "sort": "none"}}
    return _panel("xychart", title, grid, queries, description=description, defaults=defaults,
                  overrides=[x_override, *(overrides or [])], options=options)


def state_timeline(title: str, query: str, grid: dict, *, display_name: str = "${__field.labels.asset}",
                   description: str = "", interval: str | None = None) -> dict:
    defaults = {"custom": {"fillOpacity": 80, "lineWidth": 0, "spanNulls": False},
                "color": {"mode": "thresholds"}, "thresholds": thresholds((None, "text")),
                "mappings": state_mappings(), "displayName": display_name}
    options = {"mergeValues": True, "showValue": "never", "alignValue": "left", "rowHeight": 0.85,
               "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
               "tooltip": {"mode": "single", "sort": "none"}}
    extra = {"interval": interval} if interval else {}
    return _panel("state-timeline", title, grid, query, description=description, defaults=defaults,
                  options=options, **extra)


def table(title: str, queries: list[str] | str, grid: dict, *, overrides: list | None = None,
          transformations: list | None = None, sort_by: str | None = None, description: str = "",
          no_value: str = "") -> dict:
    defaults = {"custom": {"align": "auto", "cellOptions": {"type": "auto"}, "filterable": False},
                "color": {"mode": "thresholds"}, "thresholds": thresholds((None, "text"))}
    if no_value:
        defaults["noValue"] = no_value
    options = {"showHeader": True, "cellHeight": "sm",
               "footer": {"show": False, "reducer": ["sum"], "countRows": False, "fields": ""},
               "sortBy": [{"displayName": sort_by, "desc": True}] if sort_by else []}
    return _panel("table", title, grid, queries, description=description, defaults=defaults, overrides=overrides,
                  options=options, transformations=transformations or [])


def bargauge(title: str, query: str, unit: str, grid: dict, *, steps=None, min_: float = 0, max_: float = 1,
             display_name: str = "${__field.labels.asset}", description: str = "") -> dict:
    defaults = {"unit": unit, "min": min_, "max": max_, "color": {"mode": "thresholds"},
                "thresholds": thresholds(*(steps or [(None, "green")])), "displayName": display_name}
    options = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
               "orientation": "horizontal", "displayMode": "basic", "valueMode": "color", "showUnfilled": True,
               "namePlacement": "top", "sizing": "auto", "minVizHeight": 16, "minVizWidth": 8, "maxVizHeight": 300,
               "text": {"titleSize": 13, "valueSize": 16}}
    return _panel("bargauge", title, grid, query, description=description, defaults=defaults, options=options)


def barchart(title: str, query: str, grid: dict, *, x_field: str, unit: str = "short", description: str = "",
             color: str = "orange") -> dict:
    defaults = {"unit": unit, "color": {"mode": "fixed", "fixedColor": color},
                "custom": {"fillOpacity": 80, "lineWidth": 0, "gradientMode": "none", "axisPlacement": "auto"}}
    options = {"xField": x_field, "orientation": "horizontal", "showValue": "auto", "barWidth": 0.8,
               "groupWidth": 0.7, "stacking": "none", "xTickLabelRotation": 0, "xTickLabelSpacing": 0,
               "legend": {"displayMode": "list", "placement": "bottom", "showLegend": False},
               "tooltip": {"mode": "single", "sort": "none"}}
    return _panel("barchart", title, grid, query, description=description, defaults=defaults, options=options)


def alertlist(title: str, grid: dict, *, show_normal: bool = False, instances: bool = True,
              description: str = "") -> dict:
    options = {"viewMode": "list", "groupMode": "default", "groupBy": [], "maxItems": 20, "sortOrder": 1,
               "dashboardAlerts": False, "alertName": "", "alertInstanceLabelFilter": '{fleet="demo"}',
               "showInstances": instances,
               "stateFilter": {"firing": True, "pending": True, "noData": True, "normal": show_normal,
                               "error": True, "recovering": True}}
    return _panel("alertlist", title, grid, None, description=description, options=options)


def text(title: str, content: str, grid: dict) -> dict:
    return _panel("text", title, grid, None, options={"mode": "markdown", "content": content})


def query_variable(name: str, label: str, query: str, default: str) -> dict:
    return {"name": name, "label": label, "type": "query", "datasource": DATASOURCE, "query": query,
            "definition": query, "refresh": 1, "sort": 1, "multi": False, "includeAll": False,
            "current": {"text": default, "value": default}, "options": [], "hide": 0}


def dashboard(uid: str, title: str, panels: list[dict], *, description: str = "", variables: list | None = None,
              time_from: str = "now-1h", refresh: str = "10s", tags=("fleet",), links: list | None = None) -> dict:
    for i, p in enumerate(panels, start=1):
        p["id"] = i
    return {
        "uid": uid, "title": title, "description": description, "tags": list(tags), "editable": False,
        "graphTooltip": 1, "timezone": "browser", "schemaVersion": SCHEMA_VERSION, "version": 1,
        "time": {"from": time_from, "to": "now"}, "refresh": refresh,
        "timepicker": {"refresh_intervals": ["5s", "10s", "30s", "1m", "5m"]},
        "templating": {"list": variables or []},
        "annotations": {"list": [{"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                                  "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
                                  "name": "Annotations & Alerts", "type": "dashboard"}]},
        "links": links if links is not None else [
            {"title": "Fleet", "type": "dashboards", "tags": ["fleet"], "asDropdown": False, "includeVars": False,
             "keepTime": True, "targetBlank": False, "icon": "external link"}],
        "panels": panels,
    }
