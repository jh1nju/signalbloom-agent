"""Entry point: python -m signalbloom_agent"""

import argparse
import dataclasses
import json
import sys
from datetime import datetime
from pathlib import Path

from .agent import run, setup_logging
from .app import (CONFIG_NAME, app_dir, ask_setup_code, create_config, is_frozen,
                  pause_before_exit)
from .collectors import CollectorError
from .collectors.bandwidth import measure_download_mbps
from .collectors.devices import DeviceScanner
from .collectors.gateway import find_default_gateway
from .collectors.ping import ping
from .collectors.uptime import internet_up
from .config import DEFAULT_PATH, Config, ConfigError, load_config
from .register import NEW_CODE_MESSAGE, RegistrationError, ensure_registered
from .sender import AuthError


def collect_once(config: Config) -> int:
    """Run the collectors once, print an ingest-shaped body (docs/API.md), exit."""
    try:
        public = ping(config.public_address)
    except CollectorError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    reading = {
        "measured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "latency_ms": public["latency_ms"],
        "packet_loss_pct": public["packet_loss_pct"],
        "internet_up": internet_up(config.public_address, public),
        "download_mbps": None,  # filled in below
        "upload_mbps": None,
        "error_rate_pct": None,
    }

    print(f"Testing download speed ({config.bandwidth_test_mb} MB)...", file=sys.stderr)
    reading["download_mbps"] = measure_download_mbps(config.bandwidth_test_mb)

    print("Scanning the local network for devices (up to 30 s)...", file=sys.stderr)
    devices = DeviceScanner(config.public_address, config.device_scan_seconds).devices()

    print(json.dumps({"readings": [reading], "devices": devices}, indent=2))

    # The router result isn't part of the API reading, so it goes to stderr.
    router = config.router_address or find_default_gateway()
    if router is None:
        print("Router: not found (set router_address in config.yaml)", file=sys.stderr)
    else:
        local = ping(router)
        latency = "no reply" if local["latency_ms"] is None else f"{local['latency_ms']} ms"
        print(
            f"Router {router} (local only): {latency}, {local['packet_loss_pct']}% loss",
            file=sys.stderr,
        )
    return 0


def config_path() -> Path | str:
    """config.yaml next to the exe when frozen, else in the current directory."""
    return app_dir() / CONFIG_NAME if is_frozen() else DEFAULT_PATH


def load(path: Path | str) -> Config:
    """load_config, plus (when frozen) a relative data_dir is put next to the exe."""
    config = load_config(path)
    if is_frozen() and not config.data_dir.is_absolute():
        config = dataclasses.replace(config, data_dir=app_dir() / config.data_dir)
    return config


def first_run_setup(path: Path) -> bool:
    """Exe only: ask for the setup code and create config.yaml. False if cancelled."""
    print("Welcome to SignalBloom. This computer isn't set up yet.")
    print("Get a setup code from Account & Settings in the SignalBloom dashboard.")
    try:
        code = ask_setup_code()
    except (EOFError, KeyboardInterrupt):
        print("\nSetup cancelled.")
        return False
    create_config(path, code)
    return True


def main(argv: list[str] | None = None) -> int:
    code = _main(argv)
    if code != 0:
        pause_before_exit()  # exe only: keep the window open to read the error
    return code


def _main(argv: list[str] | None) -> int:
    parser = argparse.ArgumentParser(
        prog="signalbloom_agent",
        description="SignalBloom local agent: sends network readings to the SignalBloom cloud.",
    )
    parser.add_argument(
        "--demo-fault",
        action="store_true",
        help="make readings look like a network fault (for demos)",
    )
    parser.add_argument(
        "--collect-once",
        action="store_true",
        help="measure once, print the reading and exit (nothing is sent)",
    )
    args = parser.parse_args(argv)
    path = config_path()

    if args.collect_once:
        try:
            config = load(path)
        except ConfigError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        return collect_once(config)

    created = False
    if is_frozen() and not Path(path).is_file():
        if not first_run_setup(Path(path)):
            return 1
        created = True

    try:
        config = load(path)
        already_registered = bool(config.site_token)
        config = ensure_registered(config, path)
    except (ConfigError, RegistrationError, KeyboardInterrupt) as exc:
        if created:
            # Start fresh next time, so a bad code isn't retried forever.
            Path(path).unlink(missing_ok=True)
        if isinstance(exc, KeyboardInterrupt):
            print("Stopped.")
            return 0
        if created and str(exc) == NEW_CODE_MESSAGE:
            print(
                "Error: That setup code didn't work. It may be mistyped, already used, "
                "or expired. Get a new setup code from Account & Settings in the "
                "SignalBloom dashboard, then run SignalBloom-Agent.exe again.",
                file=sys.stderr,
            )
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1

    if already_registered:
        print(f"Already registered as site {config.site_id}.")
    else:
        print(f"Registered as site {config.site_id}.")

    setup_logging(config.data_dir)
    try:
        run(config, demo_fault=args.demo_fault)
    except KeyboardInterrupt:
        print("Stopped.")
        return 0
    except (AuthError, CollectorError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
