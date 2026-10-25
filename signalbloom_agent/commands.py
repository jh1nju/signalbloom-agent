"""Collect approved fixes and report results (docs/API.md sections 3 and 4).

Every fix is simulated. The agent never runs anything the cloud sends: a
command's type is only used to look up one of three allowed fixes, and any
other type is refused and reported as failed.

Each command runs at most once. Its state is saved to data_dir/commands.json
before the fix starts, and the result is saved before it is reported. A result
that couldn't be delivered is sent again next cycle, and the fix is not re-run.
"""

import json
import logging
import os
import tempfile
import time
from pathlib import Path

import requests

from .collectors.devices import normalise_mac
from .config import Config
from .sender import REJECTED_FILE, REREGISTER, AuthError, now_iso

log = logging.getLogger("signalbloom_agent")

# Command type -> the word used in the result message. Nothing else is allowed.
ALLOWED = {
    "restart_device": "restart",
    "isolate_device": "isolation",
    "reconnect_device": "reconnect",
}
STATE_FILE = "commands.json"
FIX_SECONDS = 2
TIMEOUT_SECONDS = 10
KEEP_DONE = 1000  # how many finished command IDs to remember

RUNNING, REPORTING, DONE = "running", "reporting", "done"

# What to do with a waiting result after the cloud answers.
RECORDED = "recorded"  # the cloud has it (or already had it): stop sending
DROP = "drop"          # the cloud will never take it: save to rejected.jsonl, stop
RETRY = "retry"        # try again next cycle


class _Unreachable(Exception):
    """The cloud couldn't be reached; stop for this cycle."""


def result_action(status: int) -> str:
    """What to do with a waiting result after the cloud answers `status`.

    401 and 403 never get here (they stop the agent). Returns RECORDED, DROP
    or RETRY.
    """
    if 200 <= status < 300 or status == 409:
        return RECORDED   # the cloud has the result (409 = it already had it)
    if status in (408, 429) or status >= 500:
        return RETRY      # timeout, rate limit or server error: try next cycle
    return DROP           # 404, 400 and other 4xx won't succeed on a retry


class CommandPoller:
    def __init__(self, config: Config, *, get=requests.get, post=requests.post,
                 now=now_iso, sleep=time.sleep):
        base = config.cloud_url.rstrip("/") + "/api/agent/commands"
        self._list_url = base
        self._result_url = base + "/{}/result"
        self._headers = {"Authorization": f"Bearer {config.site_token}"}
        self._get = get
        self._post = post
        self._now = now
        self._sleep = sleep
        self._path = Path(config.data_dir) / STATE_FILE
        self._rejected_path = Path(config.data_dir) / REJECTED_FILE
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._commands: dict[str, dict] = {}
        self._load()

    def __repr__(self) -> str:
        return f"CommandPoller(url={self._list_url!r})"  # never the token

    @property
    def waiting(self) -> int:
        """Results not reported yet."""
        return sum(1 for c in self._commands.values() if c["state"] == REPORTING)

    def poll(self, devices: list[dict]) -> int:
        """Report waiting results, then fetch and run new commands.

        Returns how many fixes ran and succeeded this cycle.
        Raises AuthError on 401/403. Everything else is logged.
        """
        succeeded = 0
        try:
            self._report_waiting()
            # The cloud marks commands as sent once listed, so every one is
            # run and saved before any reporting that could stop the cycle.
            for command in self._fetch():
                result = self._handle(command, devices)
                if result is not None and result["status"] == "succeeded":
                    succeeded += 1
            self._report_waiting()
        except _Unreachable:
            waiting = self.waiting
            note = f", {waiting} fix result{'s' if waiting != 1 else ''} waiting" if waiting else ""
            log.info(f"cloud unreachable while checking for fixes{note}")
        return succeeded

    # Fetch and run

    def _fetch(self) -> list:
        try:
            response = self._get(self._list_url, headers=self._headers, timeout=TIMEOUT_SECONDS)
        except requests.RequestException:
            raise _Unreachable from None
        self._check_auth(response.status_code)
        if response.status_code != 200:
            log.info(f"cloud error while checking for fixes (HTTP {response.status_code})")
            return []
        commands = _json(response).get("commands")
        if not isinstance(commands, list):
            log.info("cloud sent a fix list the agent couldn't read")
            return []
        return commands

    def _handle(self, command, devices: list[dict]) -> dict | None:
        """Run one command. Returns its result, or None if it was skipped."""
        if not isinstance(command, dict) or not isinstance(command.get("id"), str):
            log.info("skipped a fix with no id")
            return None
        command_id = command["id"]
        if command_id in self._commands:
            log.info(f"already handled {command_id}, skipping")
            return None

        # Saved before the fix starts, so a crash can't lead to a second run.
        self._commands[command_id] = {"state": RUNNING}
        self._save()
        result = self._run_fix(command_id, command, devices)
        self._commands[command_id] = {"state": REPORTING, "result": result}
        self._save()
        return result

    def _run_fix(self, command_id: str, command: dict, devices: list[dict]) -> dict:
        """Run one simulated fix. Never runs anything for a type not in ALLOWED."""
        kind = command.get("type")
        if not isinstance(kind, str) or kind not in ALLOWED:
            log.warning(f"refused {command_id}: unsupported command type {kind!r}, nothing was run")
            return self._result("failed", f"Unsupported command type: {kind!r}. Nothing was run.")

        target = command.get("target")
        raw_mac = target.get("mac") if isinstance(target, dict) else None
        mac = normalise_mac(raw_mac) if isinstance(raw_mac, str) else None
        if mac is None:
            log.info(f"{command_id} has no valid target MAC")
            return self._result("failed", "Command has no valid target MAC. Nothing was done.")

        device = next((d for d in devices
                       if isinstance(d.get("mac"), str) and normalise_mac(d["mac"]) == mac), None)
        if device is None:
            log.info(f"{command_id}: device {mac} is not on this network")
            return self._result("failed", f"Device {mac} is not on this network. Nothing was done.")

        log.info(f"running simulated {ALLOWED[kind]} for {command_id} on {device.get('ip')} ({mac})")
        self._sleep(FIX_SECONDS)
        return self._result(
            "succeeded", f"Simulated {ALLOWED[kind]} of {device.get('ip')} ({mac}) completed.")

    def _result(self, status: str, message: str) -> dict:
        return {"status": status, "finished_at": self._now(), "message": message}

    # Report

    def _report_waiting(self) -> None:
        for command_id in [i for i, c in self._commands.items() if c["state"] == REPORTING]:
            self._report(command_id)

    def _report(self, command_id: str) -> None:
        result = self._commands[command_id]["result"]
        try:
            response = self._post(self._result_url.format(command_id), json=result,
                                  headers=self._headers, timeout=TIMEOUT_SECONDS)
        except requests.RequestException:
            raise _Unreachable from None
        status = response.status_code
        self._check_auth(status)

        action = result_action(status)
        if action == RETRY:
            log.info(f"cloud error reporting {command_id} (HTTP {status}), will retry")
            return
        if action == DROP:
            message = _error_message(response)
            detail = f"HTTP {status}: {message}" if message else f"HTTP {status}"
            log.info(f"cloud refused the result for {command_id} ({detail}), "
                     f"dropped and saved to {REJECTED_FILE}")
            self._save_rejected(command_id, result, status, message)
        elif status == 409:
            log.info(f"result for {command_id} was already recorded")
        else:
            log.info(f"reported {command_id}: {result['status']}")
        self._commands[command_id] = {"state": DONE}
        self._forget_old()
        self._save()

    def _check_auth(self, status: int) -> None:
        if status == 401:
            raise AuthError("The cloud no longer accepts this agent's site token (HTTP 401). "
                            + REREGISTER)
        if status == 403:
            raise AuthError("The cloud says this site token belongs to another site "
                            "(HTTP 403). " + REREGISTER)

    def _save_rejected(self, command_id: str, result: dict, status: int, message: str) -> None:
        record = {"rejected_at": self._now(), "status": status, "message": message,
                  "command_id": command_id, "result": result}
        with open(self._rejected_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    # State file

    def _forget_old(self) -> None:
        done = [i for i, c in self._commands.items() if c["state"] == DONE]
        for command_id in done[:max(0, len(done) - KEEP_DONE)]:
            del self._commands[command_id]

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            commands = data["commands"]
            if not isinstance(commands, dict):
                raise TypeError
        except FileNotFoundError:
            return
        except (ValueError, KeyError, TypeError):
            log.warning(f"could not read {self._path}, starting with no fix history")
            return
        for command_id, entry in commands.items():
            if not isinstance(entry, dict):
                continue
            state = entry.get("state")
            if state == REPORTING and isinstance(entry.get("result"), dict):
                self._commands[command_id] = {"state": REPORTING, "result": entry["result"]}
            elif state == RUNNING:
                # The agent stopped mid-fix. Don't run it again; report it as failed.
                self._commands[command_id] = {"state": REPORTING, "result": self._result(
                    "failed", "Agent stopped before the simulated fix finished.")}
            else:
                self._commands[command_id] = {"state": DONE}
        self._save()

    def _save(self) -> None:
        text = json.dumps({"commands": self._commands})
        fd, tmp_name = tempfile.mkstemp(dir=self._path.parent, prefix=".commands-", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            os.replace(tmp, self._path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise


def _json(response) -> dict:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _error_message(response) -> str:
    try:
        return str(_json(response)["error"]["message"])
    except (KeyError, TypeError):
        return ""
