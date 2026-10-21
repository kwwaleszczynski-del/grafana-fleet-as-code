"""Check a running stack through Grafana's HTTP API: datasource health, provisioned dashboards and alert rules,
and every panel query executed by Grafana itself (/api/ds/query), so Flux errors surface without a browser.

Usage:
    from fleet_as_code.verify import verify
    problems = verify("http://127.0.0.1:3000", ("admin", password))
"""
from __future__ import annotations

import logging

import requests

from fleet_as_code import config
from fleet_as_code.grafana.alerts import rules
from fleet_as_code.grafana.dashboards import build_all

log = logging.getLogger(__name__)


def _rows(result: dict) -> int:
    return sum(len(f["data"]["values"][0]) if f.get("data", {}).get("values") else 0 for f in result.get("frames", []))


def panel_queries(speed: float = config.SPEED):
    """(dashboard uid, panel title, refId, query) with dashboard variables set to their defaults."""
    for d in build_all(speed):
        defaults = {v["name"]: v["current"]["value"] for v in d["templating"]["list"]}
        for p in d["panels"]:
            for t in p.get("targets", []):
                q = t["query"]
                for name, value in defaults.items():
                    q = q.replace("${" + name + "}", value)
                yield d["uid"], p["title"], t["refId"], q


def verify(url: str, auth: tuple[str, str], time_from: str = "now-1h", speed: float = config.SPEED) -> list[str]:
    s = requests.Session()
    s.auth = auth
    url = url.rstrip("/")
    problems = []

    def get(path):
        r = s.get(url + path, timeout=30)
        r.raise_for_status()
        return r.json()

    log.info("Grafana %s", get("/api/health").get("version"))
    health = s.get(f"{url}/api/datasources/uid/{config.DATASOURCE_UID}/health", timeout=30).json()
    log.info("datasource: %s", health.get("message"))
    if health.get("status") != "OK":
        problems.append(f"datasource unhealthy: {health}")

    for d in build_all(speed):
        r = s.get(f"{url}/api/dashboards/uid/{d['uid']}", timeout=30)
        if not r.ok:
            problems.append(f"dashboard {d['uid']} not provisioned (HTTP {r.status_code})")

    provisioned = {r["uid"] for r in get("/api/v1/provisioning/alert-rules")}
    for r in rules(speed):
        if r.uid not in provisioned:
            problems.append(f"alert rule {r.uid} not provisioned")

    empty = 0
    for uid, title, ref, query in panel_queries(speed):
        body = {"from": time_from, "to": "now", "queries": [
            {"refId": ref, "datasource": {"type": "influxdb", "uid": config.DATASOURCE_UID}, "query": query,
             "intervalMs": 10_000, "maxDataPoints": 1000}]}
        res = s.post(f"{url}/api/ds/query", json=body, timeout=60).json().get("results", {}).get(ref, {})
        if res.get("error"):
            problems.append(f"{uid} / {title} / {ref}: {res['error'][:200]}")
            log.error("%-15s %-34s %s ERROR %s", uid, title, ref, res["error"][:120])
        else:
            n = _rows(res)
            empty += n == 0
            log.info("%-15s %-34s %s %6d rows", uid, title, ref, n)

    for group in get("/api/prometheus/grafana/api/v1/rules")["data"]["groups"]:
        for rule in group["rules"]:
            states = [a["state"] for a in rule.get("alerts", [])]
            log.info("rule %-32s health=%s state=%s instances=%s", rule["name"], rule.get("health"),
                     rule.get("state"), states)
            if rule.get("health") == "error":
                problems.append(f"alert rule {rule['name']}: {rule.get('lastError')}")
    log.info("%d problems, %d panel queries without rows", len(problems), empty)
    return problems
