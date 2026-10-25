"""The agent's main loop: measure every interval_seconds, buffer, send,
then collect approved fixes.

Device scans and the bandwidth test run in background threads, so a slow
scan or download never delays a send.
"""

import logging
import threading
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__
from .buffer import ReadingBuffer, cap_for
from .commands import CommandPoller
from .collectors.bandwidth import BandwidthTest
from .collectors.devices import DeviceScanner
from .collectors.ping import ping
from .collectors.uptime import internet_up
from .config import Config
from .demo import DemoFault
from .schedule import Ticker, wait_until_due
from .sender import Sender

log = logging.getLogger("signalbloom_agent")

LOG_FILE = "agent.log"
LOG_MAX_BYTES = 1_000_000
LOG_BACKUPS = 3


def setup_logging(data_dir: str | Path) -> None:
    """One line per event to the console and to data_dir/agent.log."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler()
    file = RotatingFileHandler(data_dir / LOG_FILE, maxBytes=LOG_MAX_BYTES,
                               backupCount=LOG_BACKUPS, encoding="utf-8")
    for handler in (console, file):
        handler.setFormatter(formatter)
    for old in list(log.handlers):
        log.removeHandler(old)
        old.close()
    log.addHandler(console)
    log.addHandler(file)
    log.setLevel(logging.INFO)
    log.propagate = False


def build_reading(public_address: str) -> dict:
    """One reading in the docs/API.md shape. download_mbps is filled in by the caller.

    Raises CollectorError if ping can't run at all.
    """
    measured_at = datetime.now().astimezone().isoformat(timespec="seconds")
    public = ping(public_address)
    return {
        "measured_at": measured_at,
        "latency_ms": public["latency_ms"],
        "packet_loss_pct": public["packet_loss_pct"],
        "internet_up": internet_up(public_address, public),
        "download_mbps": None,
        "upload_mbps": None,
        "error_rate_pct": None,
    }


class DeviceWorker:
    """Keeps the device list fresh in a background thread.

    Calls DeviceScanner.devices() every recheck_seconds; the scanner itself
    does a full scan instead when device_scan_seconds have passed.
    """

    def __init__(self, scanner, recheck_seconds: int, *, clock=time.monotonic):
        self._scanner = scanner
        self._recheck_seconds = recheck_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._devices: list[dict] = []
        self._scan_id = 0  # goes up by one after each full scan
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="device-scan", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def snapshot(self) -> list[dict]:
        return self.current()[0]

    def current(self) -> tuple[list[dict], int]:
        """The device list and the id of the full scan it came from, read together."""
        with self._lock:
            return [dict(d) for d in self._devices], self._scan_id

    def run_once(self) -> None:
        before = getattr(self._scanner, "last_full_scan", None)
        try:
            found = self._scanner.devices()
        except Exception as exc:  # a failed scan must not kill the thread
            log.info(f"device scan failed: {exc}")
            return
        full_scan = getattr(self._scanner, "last_full_scan", None) != before
        with self._lock:
            self._devices = found
            if full_scan:
                self._scan_id += 1

    def _loop(self) -> None:
        ticker = Ticker(self._recheck_seconds, self._clock)
        while not self._stop.is_set():
            self.run_once()
            ticker.advance()
            self._stop.wait(ticker.seconds_until_due())


class BandwidthWorker:
    """Runs the bandwidth test in a background thread when it's due."""

    def __init__(self, test: BandwidthTest):
        self._test = test
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._result: float | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start_if_due(self) -> bool:
        if self.running or not self._test.due():
            return False
        self._thread = threading.Thread(target=self._run, name="bandwidth", daemon=True)
        self._thread.start()
        return True

    def take_result(self) -> float | None:
        """The finished test's speed, once. None if nothing new (or the test failed)."""
        with self._lock:
            result, self._result = self._result, None
        return result

    def _run(self) -> None:
        try:
            mbps = self._test.maybe_run()
        except Exception as exc:
            log.info(f"bandwidth test failed: {exc}")
            mbps = None
        with self._lock:
            self._result = mbps


def run(config: Config, *, stop: threading.Event | None = None, clock=time.monotonic,
        sleep=time.sleep, measure=None, buffer=None, sender=None,
        devices=None, bandwidth=None, poller=None, demo=None,
        demo_fault: bool = False) -> None:
    """Measure and send until `stop` is set or Ctrl+C (KeyboardInterrupt).

    Everything after `config` can be swapped out in tests.
    Raises AuthError if the cloud refuses the token.
    """
    stop = stop or threading.Event()
    measure = measure or (lambda: build_reading(config.public_address))
    buffer = buffer if buffer is not None else ReadingBuffer(
        config.data_dir, cap_for(config.interval_seconds))
    sender = sender or Sender(config, buffer)
    devices = devices or DeviceWorker(
        DeviceScanner(config.public_address, config.device_scan_seconds),
        config.device_recheck_seconds)
    bandwidth = bandwidth or BandwidthWorker(
        BandwidthTest(config.bandwidth_test_mb, config.bandwidth_interval_seconds))
    poller = poller or CommandPoller(config)
    demo = demo or DemoFault(demo_fault, config.data_dir,
                             config.demo_fault_latency_ms, config.demo_fault_loss_pct)

    if getattr(buffer, "bad_lines", 0):
        log.info(f"skipped {buffer.bad_lines} unreadable lines in the buffer file")
    if len(buffer):
        log.info(f"{len(buffer)} readings from an earlier run waiting to be sent")
    if getattr(poller, "waiting", 0):
        log.info(f"{poller.waiting} fix results from an earlier run waiting to be reported")
    log.info(f"SignalBloom agent v{__version__} running as site {config.site_id}, "
             f"sending every {config.interval_seconds} s. Press Ctrl+C to stop.")

    ticker = Ticker(config.interval_seconds, clock)
    devices.start()
    try:
        while not stop.is_set():
            speed_test_running = bandwidth.running
            reading = measure()
            reading["download_mbps"] = bandwidth.take_result()
            reading = demo.process(reading)  # before buffering, so resends match
            bandwidth.start_if_due()  # straight after the ping, so it rarely overlaps one
            if speed_test_running:
                log.info("latency may be raised by the speed test")

            dropped = buffer.add(reading)
            if dropped:
                log.info(f"buffer full, dropped {dropped} oldest "
                         + ("reading" if dropped == 1 else "readings"))

            device_list, scan_id = devices.current()
            summary = sender.flush(device_list, scan=scan_id)
            summary.latest = reading
            for line in summary.lines():
                log.info(line)
            # After the send; may take a few seconds per fix.
            if poller.poll(device_list):
                demo.clear_by_fix()

            skipped = ticker.advance()
            if skipped:
                log.info(f"running behind, skipped {skipped} "
                         + ("reading" if skipped == 1 else "readings"))
            wait_until_due(ticker, sleep, stop)
    finally:
        devices.stop()
