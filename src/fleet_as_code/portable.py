"""Run the stack without Docker: official Grafana OSS and InfluxDB 2 binaries in .portable/, bound to 127.0.0.1.

Meant for machines where Docker is not available (no admin rights, no WSL). The archives are checked against the
SHA-256 published by Grafana Labs and InfluxData before they are unpacked. Grafana reads the same provisioning
files as the container; only paths and URLs come from the environment.

Usage:
    python -m fleet_as_code portable fetch     # download + verify + unpack (~300 MB, once)
    python -m fleet_as_code portable up        # start both servers until Ctrl+C
"""
from __future__ import annotations

import hashlib
import logging
import os
import platform
import subprocess
import tarfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import requests

from fleet_as_code import config
from fleet_as_code.influx import load_env

log = logging.getLogger(__name__)

HOME = config.ROOT / ".portable"


@dataclass(frozen=True)
class Package:
    name: str
    url: str
    sha256: str
    binary: str          # path of the server executable inside the unpacked folder


GRAFANA_VERSION, INFLUX_VERSION = config.GRAFANA_VERSION, config.INFLUX_VERSION
PACKAGES = {
    "Windows": [
        Package("grafana", f"https://dl.grafana.com/grafana/release/{GRAFANA_VERSION}/"
                           f"grafana_{GRAFANA_VERSION}_38252365904_windows_amd64.tar.gz",
                "3e005ff50fe3bb6b8cb428ef127636fac4ee676bf9b6d819c6a2297d21f4bcbd", "bin/grafana.exe"),
        Package("influxdb", f"https://dl.influxdata.com/influxdb/releases/influxdb2-{INFLUX_VERSION}-windows_amd64.zip",
                "d8b93916a5e21fe348dfdb8de7ea04fb7425b38939486e874cd8096463b91043", "influxd.exe"),
    ],
    "Linux": [
        Package("grafana", f"https://dl.grafana.com/grafana/release/{GRAFANA_VERSION}/"
                           f"grafana_{GRAFANA_VERSION}_38252365904_linux_amd64.tar.gz",
                "c48765988fd88572607e4ea670251af8a8e64ab3e360d0f3cf67a6f3587b92ab", "bin/grafana"),
        Package("influxdb", f"https://dl.influxdata.com/influxdb/releases/influxdb2-{INFLUX_VERSION}_linux_amd64.tar.gz",
                "762e4fc825c4386e0c5138e7c3f91fc778081db2bada1ec47066e786bf55d9ff", "influxd"),
    ],
}


def packages() -> list[Package]:
    try:
        return PACKAGES[platform.system()]
    except KeyError:
        raise SystemExit(f"no portable packages for {platform.system()}; use docker compose") from None


def _download(pkg: Package, dest: Path) -> Path:
    archive = dest / pkg.url.rsplit("/", 1)[1]
    if not archive.exists():
        log.info("downloading %s", pkg.url)
        tmp = archive.with_suffix(".part")
        with requests.get(pkg.url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        tmp.replace(archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != pkg.sha256:
        archive.unlink()
        raise SystemExit(f"{archive.name}: SHA-256 mismatch ({digest}), file deleted")
    return archive


def _unpack(archive: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as z:
            for name in z.namelist():   # refuse absolute paths and ../ entries
                if not (target / name).resolve().is_relative_to(target.resolve()):
                    raise SystemExit(f"unsafe path in {archive.name}: {name}")
            z.extractall(target)
    else:
        with tarfile.open(archive) as t:
            t.extractall(target, filter="data")


def install_dir(pkg: Package) -> Path:
    """Folder holding pkg.binary (archives wrap their content in a versioned top-level folder)."""
    root = HOME / pkg.name
    for candidate in [root, *sorted(p for p in root.glob("*") if p.is_dir())]:
        if (candidate / pkg.binary).exists():
            return candidate
    raise SystemExit(f"{pkg.name} is not installed, run: python -m fleet_as_code portable fetch")


def fetch() -> None:
    downloads = HOME / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    for pkg in packages():
        archive = _download(pkg, downloads)
        if not (HOME / pkg.name).exists():
            _unpack(archive, HOME / pkg.name)
        log.info("%s ready in %s", pkg.name, install_dir(pkg))


def up(grafana_port: int = 3000, influx_port: int = 8086) -> None:
    load_env()
    for key in ("INFLUX_TOKEN", "GF_SECURITY_ADMIN_PASSWORD"):
        if not os.environ.get(key):
            raise SystemExit(f"{key} is not set: copy .env.example to .env and fill it in")
    grafana, influx = (install_dir(p) for p in packages())
    data = HOME / "state"
    env = {**os.environ,
           "INFLUX_URL": f"http://127.0.0.1:{influx_port}",
           "FLEET_DASHBOARDS_DIR": str(config.GRAFANA_DIR / "dashboards"),
           "GF_PATHS_PROVISIONING": str(config.GRAFANA_DIR / "provisioning"),
           "GF_PATHS_DATA": str(data / "grafana"), "GF_PATHS_LOGS": str(data / "grafana" / "log"),
           "GF_PATHS_PLUGINS": str(data / "grafana" / "plugins"),
           "GF_SERVER_HTTP_ADDR": "127.0.0.1", "GF_SERVER_HTTP_PORT": str(grafana_port),
           "GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH": str(config.GRAFANA_DIR / "dashboards" / "fleet-overview.json")}
    env.setdefault("GF_AUTH_ANONYMOUS_ENABLED", "true")
    env.setdefault("GF_AUTH_ANONYMOUS_ORG_ROLE", "Viewer")
    env.setdefault("GF_ANALYTICS_REPORTING_ENABLED", "false")
    env.setdefault("GF_ANALYTICS_CHECK_FOR_UPDATES", "false")
    env.setdefault("GF_NEWS_NEWS_FEED_ENABLED", "false")
    (data / "influxdb").mkdir(parents=True, exist_ok=True)
    (data / "grafana").mkdir(parents=True, exist_ok=True)
    procs = []
    try:
        with open(data / "influxd.log", "ab") as influx_log, open(data / "grafana.log", "ab") as grafana_log:
            procs.append(subprocess.Popen(
                [str(influx / packages()[1].binary), "--bolt-path", str(data / "influxdb" / "influxd.bolt"),
                 "--engine-path", str(data / "influxdb" / "engine"), "--http-bind-address", f"127.0.0.1:{influx_port}",
                 "--reporting-disabled"], stdout=influx_log, stderr=subprocess.STDOUT, env=env))
            procs.append(subprocess.Popen(
                [str(grafana / packages()[0].binary), "server", "--homepath", str(grafana)],
                cwd=grafana, stdout=grafana_log, stderr=subprocess.STDOUT, env=env))
            log.info("InfluxDB on http://127.0.0.1:%d, Grafana on http://127.0.0.1:%d (logs in %s), Ctrl+C stops",
                     influx_port, grafana_port, data)
            while all(p.poll() is None for p in procs):
                time.sleep(1)
            log.error("a server exited, see the logs in %s", data)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            if p.poll() is None:
                if platform.system() == "Windows":   # whole tree: Grafana runs its datasource plugins as children
                    subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
                else:
                    p.terminate()
        for p in procs:
            try:
                p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                p.kill()
        log.info("stopped")
