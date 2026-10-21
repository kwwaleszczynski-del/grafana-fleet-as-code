"""Command line interface.

Usage:
    python -m fleet_as_code generate                      # dashboards, alert rules, datasource -> grafana/
    python -m fleet_as_code generate --check              # exit 1 if grafana/ is out of date
    python -m fleet_as_code prepare --kelmarsh-dir data/kelmarsh --pvdaq-dir data/pvdaq/system_34
    python -m fleet_as_code replay --reset                # stream data/replay into InfluxDB at 60x
    python -m fleet_as_code replay --data tests/data --speed 600 --backfill-h 24   # small committed sample
    python -m fleet_as_code replay --fault derate:KWF4:2:12:0.7 --fault blackout:KWF2:6:3
    python -m fleet_as_code verify                        # query every panel through Grafana's API
    python -m fleet_as_code portable fetch | up           # run Grafana + InfluxDB without Docker

Connection settings and passwords come from .env (see .env.example).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import pandas as pd

from fleet_as_code import config

log = logging.getLogger("fleet_as_code")


def cmd_generate(args) -> int:
    from fleet_as_code.grafana.provisioning import render, stale, write
    files = render(args.speed)
    if args.check:
        out = stale(files, args.out)
        for rel in out:
            log.error("out of date: %s", rel)
        if not out:
            log.info("%d files up to date", len(files))
        return 1 if out else 0
    write(files, args.out)
    log.info("wrote %d files to %s (speed %gx)", len(files), args.out, args.speed)
    return 0


def cmd_prepare(args) -> int:
    from fleet_as_code.prepare import prepare, write_sample
    if args.sample:
        write_sample(args.out, config.SAMPLE_DIR, args.sample_wind_start, args.sample_pv_start, args.sample_days)
        return 0
    prepare(args.kelmarsh_dir, args.pvdaq_dir, args.out, tuple(args.wind_window), tuple(args.pv_window),
            args.reference_year)
    return 0


def write_reference(db) -> None:
    from fleet_as_code.influx import line
    curves = pd.read_csv(config.REFERENCE_CURVE)
    lines = [line("power_curve", {"model": config.WIND_MODEL, "asset": asset},
                  {"wind_speed": r.wind_speed, "power_kw": r.power_kw, "samples": int(r.samples)}, ts_ms=i * 1000)
             for asset, curve in curves.groupby("asset") for i, r in enumerate(curve.itertuples())]
    db.write(config.REFERENCE_BUCKET, lines)
    log.info("reference curves: %d bins written to bucket %s", len(lines), config.REFERENCE_BUCKET)


def cmd_setup(args) -> int:
    from fleet_as_code.influx import Influx
    db = Influx.from_env()
    db.ensure_setup()
    write_reference(db)
    return 0


def cmd_replay(args) -> int:
    from fleet_as_code.influx import Influx
    from fleet_as_code.replay import Fault, Source, build_lines, run
    db = Influx.from_env()
    db.ensure_setup()
    write_reference(db)
    if args.reset:
        db.delete_all(config.BUCKET)
        log.info("bucket %s emptied", config.BUCKET)
    d = args.data
    sources = [Source.wind(pd.read_parquet(d / "wind.parquet"), pd.read_parquet(d / "wind_events.parquet"),
                           args.wind_start),
               Source.pv(pd.read_parquet(d / "pv.parquet"), pd.read_parquet(d / "pv_events.parquet"), args.pv_start)]
    faults = [Fault.parse(f) for f in args.fault]
    t0 = time.time()
    times, lines = build_lines(sources, args.speed, t0, args.backfill_h, args.duration_h, faults)
    log.info("%d lines, %.0f s of plant history backfilled, %.1f min of live replay at %gx%s", len(lines),
             args.backfill_h * 3600, (times[-1] - t0) / 60 if len(times) else 0, args.speed,
             f", faults: {', '.join(args.fault)}" if faults else "")
    try:
        run(db, times, lines, sources, args.speed, t0, tick=args.tick)
    except KeyboardInterrupt:
        log.info("stopped")
    return 0


def cmd_verify(args) -> int:
    from fleet_as_code.influx import load_env
    from fleet_as_code.verify import verify
    load_env()
    password = os.environ.get("GF_SECURITY_ADMIN_PASSWORD")
    if not password:
        raise SystemExit("GF_SECURITY_ADMIN_PASSWORD is not set")
    problems = verify(os.environ.get("GRAFANA_URL", "http://127.0.0.1:3000"),
                      (os.environ.get("GF_SECURITY_ADMIN_USER", "admin"), password), args.time_from, args.speed)
    for p in problems:
        log.error(p)
    return 1 if problems else 0


def cmd_portable(args) -> int:
    from fleet_as_code import portable
    portable.fetch() if args.action == "fetch" else portable.up()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="fleet-as-code", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("generate", help="render grafana/ from Python")
    p.add_argument("--out", type=Path, default=config.GRAFANA_DIR)
    p.add_argument("--speed", type=float, default=config.SPEED, help="replay speed the alert windows assume")
    p.add_argument("--check", action="store_true", help="only compare with the files on disk")

    p = sub.add_parser("prepare", help="raw downloads -> replay parquet files + reference curve")
    p.add_argument("--kelmarsh-dir", type=Path, default=Path("data/kelmarsh"), help="folder with Kelmarsh_SCADA_<year>_*.zip")
    p.add_argument("--pvdaq-dir", type=Path, default=Path("data/pvdaq/system_34"), help="PVDAQ daily CSV files")
    p.add_argument("--out", type=Path, default=Path("data/replay"))
    p.add_argument("--wind-window", nargs=2, default=["2020-10-15", "2020-11-15"], metavar=("START", "END"))
    p.add_argument("--pv-window", nargs=2, default=["2016-05-25", "2016-06-25"], metavar=("START", "END"))
    p.add_argument("--reference-year", type=int, default=2019)
    p.add_argument("--sample", action="store_true", help="cut the committed test sample from --out instead")
    p.add_argument("--sample-wind-start", default="2020-10-23 12:00")
    p.add_argument("--sample-pv-start", default="2016-06-03 08:00")
    p.add_argument("--sample-days", type=float, default=2.0)

    sub.add_parser("setup", help="onboard InfluxDB (portable mode), create buckets, write the reference curve")

    p = sub.add_parser("replay", help="stream the replay files into InfluxDB on an accelerated clock")
    p.add_argument("--data", type=Path, default=Path("data/replay"))
    p.add_argument("--wind-start", default="2020-10-24 02:00", help="UTC plant time mapped to now")
    p.add_argument("--pv-start", default="2016-06-04 04:00", help="UTC plant time mapped to now")
    p.add_argument("--speed", type=float, default=config.SPEED, help="plant seconds per wall second")
    p.add_argument("--backfill-h", type=float, default=48.0, help="plant hours written at once before the start")
    p.add_argument("--duration-h", type=float, default=24 * 14, help="plant hours to replay after the start")
    p.add_argument("--fault", action="append", default=[], metavar="KIND:ASSET:START_H:HOURS[:FACTOR]",
                   help="inject blackout/derate faults, plant hours after the start (repeatable)")
    p.add_argument("--reset", action="store_true", help=f"delete all data in bucket {config.BUCKET} first")
    p.add_argument("--tick", type=float, default=1.0, help="wall seconds between writes")

    p = sub.add_parser("verify", help="check the running stack through Grafana's API")
    p.add_argument("--time-from", default="now-1h")
    p.add_argument("--speed", type=float, default=config.SPEED)

    p = sub.add_parser("portable", help="Grafana + InfluxDB binaries without Docker")
    p.add_argument("action", choices=["fetch", "up"])

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    handlers = {"generate": cmd_generate, "prepare": cmd_prepare, "setup": cmd_setup, "replay": cmd_replay,
                "verify": cmd_verify, "portable": cmd_portable}
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
