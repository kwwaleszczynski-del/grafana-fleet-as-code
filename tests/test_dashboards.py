"""Static checks of everything Grafana loads: the committed files match the generator, panels fit the grid,
queries only use the InfluxDB schema in config, alert rules are well formed and no secret is written to disk."""
import json
import re

import pytest
import yaml

from fleet_as_code import config
from fleet_as_code.grafana import flux
from fleet_as_code.grafana.alerts import EVAL_INTERVAL_S, rules_file
from fleet_as_code.grafana.dashboards import build_all
from fleet_as_code.grafana.provisioning import render, stale

DASHBOARDS = build_all()
PANELS = [(d["uid"], p) for d in DASHBOARDS for p in d["panels"]]
ALL_FIELDS = {f for fields in config.MEASUREMENTS.values() for f in fields}
TAGS = {"asset", "site", "severity", "category", "model"}


def test_committed_files_are_up_to_date():
    assert stale(render(), config.GRAFANA_DIR) == [], "run: python -m fleet_as_code generate"


def test_rendered_files_parse():
    for rel, content in render().items():
        if rel.endswith(".gitkeep"):
            continue
        doc = json.loads(content) if rel.endswith(".json") else yaml.safe_load(content)
        assert doc


def test_uids_and_panel_ids_unique():
    uids = [d["uid"] for d in DASHBOARDS]
    assert len(uids) == len(set(uids)) == 4
    for d in DASHBOARDS:
        ids = [p["id"] for p in d["panels"]]
        assert len(ids) == len(set(ids))
        titles = [p["title"] for p in d["panels"]]
        assert len(titles) == len(set(titles)), f"{d['uid']}: alert rules link panels by title"


@pytest.mark.parametrize("dash", DASHBOARDS, ids=lambda d: d["uid"])
def test_panels_fit_grid_without_overlap(dash):
    cells = set()
    for p in dash["panels"]:
        g = p["gridPos"]
        assert 0 <= g["x"] and g["x"] + g["w"] <= 24 and g["h"] > 0
        new = {(x, y) for x in range(g["x"], g["x"] + g["w"]) for y in range(g["y"], g["y"] + g["h"])}
        assert not cells & new, f"{p['title']} overlaps another panel"
        cells |= new


def _queries():
    for uid, p in PANELS:
        for t in p.get("targets", []):
            yield f"{uid}/{p['title']}/{t['refId']}", t["query"]
    for rule in rules_file()["groups"][0]["rules"]:
        yield f"rule/{rule['uid']}", rule["data"][0]["model"]["query"]


QUERIES = list(_queries())


def test_targets_use_provisioned_datasource():
    for _, p in PANELS:
        for t in p.get("targets", []):
            assert t["datasource"]["uid"] == config.DATASOURCE_UID


@pytest.mark.parametrize("name,query", QUERIES, ids=[q[0] for q in QUERIES])
def test_queries_match_schema(name, query):
    assert query.count("(") == query.count(")") and query.count("[") == query.count("]")
    assert query.count("{") == query.count("}")
    buckets = set(re.findall(r'from\(bucket: "([^"]+)"\)', query))
    assert buckets and buckets <= {config.BUCKET, config.REFERENCE_BUCKET}
    assert "range(" in query
    measurements = set(re.findall(r'r\._measurement == "([^"]+)"', query))
    assert measurements and measurements <= set(config.MEASUREMENTS)
    allowed = {f for m in measurements for f in config.MEASUREMENTS[m]}
    assert set(re.findall(r'r\._field == "([^"]+)"', query)) <= allowed
    # columns produced by pivot() must be real fields or tags
    for col in re.findall(r"exists r\.(\w+)", query):
        assert col in allowed
    for col in re.findall(r"r\.(\w+) (?:==|>=|>|<) ", query):
        assert col in allowed | TAGS | {"_measurement", "_field", "expected", "h", "n"}   # reduce() accumulators


def test_variables_are_defined():
    for d in DASHBOARDS:
        names = {v["name"] for v in d["templating"]["list"]}
        used = set(re.findall(r"\$\{(\w+)\}", json.dumps(d["panels"])))
        assert used - {"__value", "__url_time_range"} <= names


def test_links_point_to_existing_dashboards():
    uids = {d["uid"] for d in DASHBOARDS}
    for _, p in PANELS:
        for override in p["fieldConfig"]["overrides"]:
            for prop in override["properties"]:
                if prop["id"] == "links":
                    for link in prop["value"]:
                        assert re.match(r"/d/([\w-]+)", link["url"]).group(1) in uids


def test_state_mappings_cover_all_codes():
    p = next(p for _, p in PANELS if p["type"] == "state-timeline")
    assert set(p["fieldConfig"]["defaults"]["mappings"][0]["options"]) == {str(c) for c in config.STATES}


@pytest.mark.parametrize("speed", [1, 60, 600])
def test_alert_rules(speed):
    dashboards = build_all(speed)
    panel_ids = {(d["uid"], p["title"]): p["id"] for d in dashboards for p in d["panels"]}
    doc = rules_file(speed, panel_ids)
    group = doc["groups"][0]
    assert group["folder"] == config.ALERT_FOLDER_TITLE != config.FOLDER_TITLE and group["interval"] == f"{EVAL_INTERVAL_S}s"
    uids = [r["uid"] for r in group["rules"]]
    assert len(uids) == len(set(uids)) == 4
    for r in group["rules"]:
        refs = [d["refId"] for d in r["data"]]
        assert refs == ["A", "B", "C"] and r["condition"] == "C"
        assert r["data"][0]["datasourceUid"] == config.DATASOURCE_UID
        assert r["data"][0]["relativeTimeRange"]["from"] >= EVAL_INTERVAL_S
        assert int(r["for"].rstrip("s")) in (0,) or int(r["for"].rstrip("s")) >= EVAL_INTERVAL_S
        assert r["noDataState"] in ("OK", "NoData", "Alerting")
        assert int(r["annotations"]["__panelId__"]) in panel_ids.values()
        assert r["annotations"]["__dashboardUid__"] in {d["uid"] for d in dashboards}


def test_alert_windows_follow_speed():
    window = lambda speed: rules_file(speed)["groups"][0]["rules"][0]["data"][0]["relativeTimeRange"]["from"]
    assert window(1) == 4 * 3600 and window(60) == 240 and window(600) == 24


def test_staleness_is_scaled_to_plant_minutes():
    assert "* 60.0 / 60.0" in flux.staleness(60) and "* 1.0 / 60.0" in flux.staleness(1)


def test_no_secrets_in_rendered_files():
    for rel, content in render().items():
        if rel.endswith(".yaml"):
            assert not re.search(r"(token|password)\s*:\s*(?!\$\{)\S", content, re.I), rel
    ds = yaml.safe_load(render()["provisioning/datasources/influxdb.yaml"])["datasources"][0]
    assert ds["secureJsonData"]["token"] == "${INFLUX_TOKEN}" and ds["url"] == "${INFLUX_URL}"


def test_versions_are_pinned_consistently():
    compose = yaml.safe_load((config.ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
    assert compose["grafana"]["image"] == f"grafana/grafana:{config.GRAFANA_VERSION}"
    assert compose["influxdb"]["image"] == f"influxdb:{config.INFLUX_VERSION}"
    assert all(p["pluginVersion"] == config.GRAFANA_VERSION for _, p in PANELS)
