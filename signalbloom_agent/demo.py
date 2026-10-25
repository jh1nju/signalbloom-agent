"""Demo fault: make readings look like a network problem, on cue.

On when the agent was started with --demo-fault, or while a file named
demo_fault exists in data_dir. The file is checked every cycle, so creating
or deleting it takes effect without a restart.

Changed readings carry no demo marker: the cloud treats them as real.
"""

import logging
from pathlib import Path

log = logging.getLogger("signalbloom_agent")

DEMO_FAULT_FILE = "demo_fault"


def apply_fault(reading, latency_ms, loss_pct):
    """Return a copy of `reading` with the demo fault applied."""
    faulty = dict(reading)  # copy, so the original reading is untouched
    if faulty.get("latency_ms") is not None:
        faulty["latency_ms"] = round(faulty["latency_ms"] + latency_ms, 1)
    if faulty.get("packet_loss_pct") is not None:
        faulty["packet_loss_pct"] = min(100, faulty["packet_loss_pct"] + loss_pct)
    return faulty


class DemoFault:
    def __init__(self, flag: bool, data_dir: str | Path, latency_ms: int, loss_pct: int):
        self._flag = flag
        self._file = Path(data_dir) / DEMO_FAULT_FILE
        self._latency_ms = latency_ms
        self._loss_pct = loss_pct
        self._was_active = False

    def active(self) -> bool:
        return self._flag or self._file.exists()

    def process(self, reading: dict) -> dict:
        """Call once per cycle. Logs when the fault turns on or off."""
        now = self.active()
        if now != self._was_active:
            log.info("DEMO FAULT ON: readings are being changed" if now
                     else "DEMO FAULT OFF: readings are back to normal")
            self._was_active = now
        if not now:
            return reading
        return apply_fault(reading, self._latency_ms, self._loss_pct)

    def clear_by_fix(self) -> None:
        """An approved fix succeeded: if the fault is on, turn it off.

        Deletes the demo_fault file and ignores --demo-fault for the rest of
        the run. Logs "DEMO FAULT OFF: cleared by approved fix". Does nothing
        when the fault is already off.
        """
        if not self.active():
            return
        self._flag = False
        try:
            self._file.unlink(missing_ok=True)
        except OSError as e:
            log.warning("could not delete %s: %s", self._file, e)
        self._was_active = False
        log.info("DEMO FAULT OFF: cleared by approved fix")
