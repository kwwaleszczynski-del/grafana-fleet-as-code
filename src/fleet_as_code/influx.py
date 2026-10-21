"""Minimal InfluxDB 2.x HTTP client and line protocol encoder (only what the replay needs).

Connection settings come from the environment or a local .env file (see .env.example); the token is never
logged.

Usage:
    from fleet_as_code.influx import Influx, line
    db = Influx.from_env()
    db.ensure_setup()                                   # onboarding (portable mode) + buckets
    db.write("fleet", [line("wind_turbine", {"asset": "KWF1"}, {"power_kw": 812.5}, ts_ms=1700000000000)])
"""
from __future__ import annotations

import logging
import math
import os
import time
from numbers import Integral, Real
from pathlib import Path

import requests

from fleet_as_code import config

log = logging.getLogger(__name__)


def load_env(path: Path = Path(".env")) -> None:
    """KEY=VALUE lines from path into os.environ; variables already set win."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, value = s.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _escape(s: str, chars: str) -> str:
    for c in chars:
        s = s.replace(c, "\\" + c)
    return s


def _field(v) -> str | None:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, Integral):
        return f"{int(v)}i"
    if isinstance(v, Real):
        v = float(v)
        return None if not math.isfinite(v) else f"{v:.7g}"
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if v is None:
        return None
    raise TypeError(f"unsupported field type {type(v)}")


def line(measurement: str, tags: dict, fields: dict, ts_ms: int | None = None) -> str | None:
    """One line of InfluxDB line protocol; NaN/None fields are skipped, None if no field is left."""
    fs = [(k, _field(v)) for k, v in fields.items()]
    fs = [f"{_escape(k, ', =')}={v}" for k, v in fs if v is not None]
    if not fs:
        return None
    head = _escape(measurement, ", ") + "".join(
        f",{_escape(k, ', =')}={_escape(str(v), ', =')}" for k, v in sorted(tags.items()) if v not in (None, ""))
    return f"{head} {','.join(fs)}" + ("" if ts_ms is None else f" {int(ts_ms)}")


class Influx:
    def __init__(self, url: str, token: str, org: str = config.INFLUX_ORG, timeout: float = 30):
        self.url, self.org, self.timeout = url.rstrip("/"), org, timeout
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Token {token}"

    @classmethod
    def from_env(cls) -> "Influx":
        load_env()
        token = os.environ.get("INFLUX_TOKEN")
        if not token:
            raise SystemExit("INFLUX_TOKEN is not set: copy .env.example to .env and fill it in")
        return cls(os.environ.get("INFLUX_URL", "http://127.0.0.1:8086"), token,
                   os.environ.get("INFLUX_ORG", config.INFLUX_ORG))

    def _req(self, method: str, path: str, **kw) -> requests.Response:
        r = self.session.request(method, self.url + path, timeout=self.timeout, **kw)
        if r.status_code >= 300:
            raise RuntimeError(f"InfluxDB {method} {path}: HTTP {r.status_code} {r.text[:300]}")
        return r

    def wait(self, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if self.session.get(self.url + "/health", timeout=5).ok:
                    return
            except requests.ConnectionError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError(f"InfluxDB at {self.url} not reachable after {timeout:.0f} s")
            time.sleep(1)

    def ensure_setup(self) -> None:
        """Onboard a fresh instance (portable mode; the Docker image does this itself) and create the buckets."""
        self.wait()
        if self.session.get(self.url + "/api/v2/setup", timeout=self.timeout).json().get("allowed"):
            password = os.environ.get("INFLUX_ADMIN_PASSWORD")
            if not password:
                raise SystemExit("INFLUX_ADMIN_PASSWORD is not set")
            token = self.session.headers["Authorization"].split(" ", 1)[1]
            self._req("POST", "/api/v2/setup", json={
                "username": os.environ.get("INFLUX_ADMIN_USER", "admin"), "password": password, "org": self.org,
                "bucket": config.BUCKET, "token": token, "retentionPeriodSeconds": 0})
            log.info("InfluxDB onboarded: org %s, bucket %s", self.org, config.BUCKET)
        for bucket in (config.BUCKET, config.REFERENCE_BUCKET):
            self.ensure_bucket(bucket)

    def ensure_bucket(self, name: str) -> None:
        r = self.session.get(self.url + "/api/v2/buckets", params={"org": self.org, "name": name}, timeout=self.timeout)
        if r.ok and r.json()["buckets"]:
            return
        if r.status_code not in (200, 404):   # 404 = no bucket with that name
            raise RuntimeError(f"InfluxDB GET /api/v2/buckets: HTTP {r.status_code} {r.text[:300]}")
        org_id = self._req("GET", "/api/v2/orgs", params={"org": self.org}).json()["orgs"][0]["id"]
        self._req("POST", "/api/v2/buckets", json={"orgID": org_id, "name": name, "retentionRules": []})
        log.info("created bucket %s", name)

    def write(self, bucket: str, lines: list[str], precision: str = "ms", chunk: int = 5000) -> int:
        for i in range(0, len(lines), chunk):
            self._req("POST", "/api/v2/write", params={"org": self.org, "bucket": bucket, "precision": precision},
                      data="\n".join(lines[i:i + chunk]).encode("utf-8"),
                      headers={"Content-Type": "text/plain; charset=utf-8"})
        return len(lines)

    def delete_all(self, bucket: str) -> None:
        self._req("POST", "/api/v2/delete", params={"org": self.org, "bucket": bucket},
                  json={"start": "1970-01-01T00:00:00Z", "stop": "2100-01-01T00:00:00Z"})

    def query(self, flux: str) -> str:
        """Annotated CSV result of a Flux query."""
        return self._req("POST", "/api/v2/query", params={"org": self.org}, json={"query": flux, "type": "flux"},
                         headers={"Accept": "application/csv"}).text
