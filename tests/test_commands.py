import json
import logging
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from signalbloom_agent.commands import STATE_FILE, CommandPoller, result_action
from signalbloom_agent.config import Config
from signalbloom_agent.sender import REJECTED_FILE, AuthError

TOKEN = "sbt_test_SECRET_do_not_print_789"
MAC = "6c:94:66:bd:f2:68"
DEVICES = [{"mac": MAC, "ip": "192.168.88.5", "hostname": None, "vendor": None, "online": True},
           {"mac": "aa:bb:cc:dd:ee:01", "ip": "192.168.88.6", "hostname": "pc",
            "vendor": None, "online": True}]
LIST_URL = "http://localhost:8080/api/agent/commands"


def make_config(data_dir: Path) -> Config:
    return Config(cloud_url="http://localhost:8080/", setup_code=None, site_id="site_8f3a2c",
                  site_token=TOKEN, interval_seconds=30, bandwidth_interval_seconds=900,
                  data_dir=data_dir)


def response(status: int, payload=None):
    r = MagicMock(status_code=status)
    r.json.return_value = payload if payload is not None else {}
    return r


def command(cid="cmd_51d0", kind="restart_device", mac=MAC):
    return {"id": cid, "type": kind, "target": {"mac": mac},
            "approved_at": "2026-11-03T14:31:10+13:00", "simulated": True}


class Cloud:
    """Stands in for requests.get/post.

    `queue` holds lists of commands to hand out, one list per GET. `result_replies`
    holds what each result POST returns (a status code, or an exception to raise).
    """

    def __init__(self, *batches, result_replies=(), get_reply=None):
        self.queue = list(batches)
        self.result_replies = list(result_replies)
        self.get_reply = get_reply
        self.gets = []
        self.posts = []  # (url, body, kwargs)

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        if self.get_reply is not None:
            if isinstance(self.get_reply, Exception):
                raise self.get_reply
            return self.get_reply
        return response(200, {"commands": self.queue.pop(0) if self.queue else []})

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs["json"], kwargs))
        reply = self.result_replies.pop(0) if self.result_replies else 200
        if isinstance(reply, Exception):
            raise reply
        return response(reply, {"recorded": True} if reply == 200 else
                        {"error": {"code": "x", "message": f"says {reply}"}})

    def reported(self):
        return [(url.rsplit("/", 2)[-2], body["status"], body["message"])
                for url, body, _ in self.posts]


@pytest.fixture
def make(tmp_path):
    def build(cloud, sleeps=None):
        sleeps = sleeps if sleeps is not None else []
        return CommandPoller(make_config(tmp_path), get=cloud.get, post=cloud.post,
                             now=lambda: "NOW", sleep=sleeps.append)
    return build


def state(tmp_path) -> dict:
    return json.loads((tmp_path / STATE_FILE).read_text(encoding="utf-8"))["commands"]


@pytest.mark.parametrize("kind, word", [("restart_device", "restart"),
                                        ("isolate_device", "isolation"),
                                        ("reconnect_device", "reconnect")])
def test_allowed_types_run_a_simulated_fix(make, tmp_path, kind, word):
    cloud = Cloud([command(kind=kind)])
    sleeps = []
    make(cloud, sleeps).poll(DEVICES)

    assert sleeps == [2]
    url, body, kwargs = cloud.posts[0]
    assert url == LIST_URL + "/cmd_51d0/result"
    assert body == {"status": "succeeded", "finished_at": "NOW",
                    "message": f"Simulated {word} of 192.168.88.5 ({MAC}) completed."}
    assert kwargs["headers"] == {"Authorization": f"Bearer {TOKEN}"}
    assert kwargs["timeout"] > 0
    assert cloud.gets[0][0] == LIST_URL
    assert cloud.gets[0][1]["headers"] == {"Authorization": f"Bearer {TOKEN}"}
    assert state(tmp_path) == {"cmd_51d0": {"state": "done"}}


def test_mac_is_matched_ignoring_case_and_dashes(make):
    cloud = Cloud([command(mac="6C-94-66-BD-F2-68")])
    make(cloud).poll(DEVICES)
    assert cloud.reported()[0][1] == "succeeded"


def test_unknown_mac_fails_without_running(make):
    cloud = Cloud([command(mac="de:ad:be:ef:00:01")])
    sleeps = []
    make(cloud, sleeps).poll(DEVICES)
    assert sleeps == []
    assert cloud.reported() == [("cmd_51d0", "failed",
                                 "Device de:ad:be:ef:00:01 is not on this network. "
                                 "Nothing was done.")]


@pytest.mark.parametrize("target", [None, {}, {"mac": "not-a-mac"}, {"mac": 7}])
def test_missing_or_bad_mac_fails(make, target):
    bad = command()
    bad["target"] = target
    cloud = Cloud([bad])
    make(cloud).poll(DEVICES)
    assert cloud.reported() == [("cmd_51d0", "failed",
                                 "Command has no valid target MAC. Nothing was done.")]


@pytest.mark.parametrize("kind", ["shell", "rm -rf /", "exec", "", None, 42, ["restart_device"]])
def test_unsupported_type_runs_nothing(make, monkeypatch, caplog, kind):
    def forbidden(*args, **kwargs):
        raise AssertionError("the agent tried to run something")

    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    caplog.set_level(logging.INFO, logger="signalbloom_agent")

    cloud = Cloud([command(kind=kind)])
    sleeps = []
    make(cloud, sleeps).poll(DEVICES)

    assert sleeps == []
    ((cid, status, message),) = cloud.reported()
    assert (cid, status) == ("cmd_51d0", "failed")
    assert message.startswith("Unsupported command type")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "unsupported command type" in warnings[0].getMessage()


def test_duplicate_id_runs_once(make, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="signalbloom_agent")
    cloud = Cloud([command(), command()], [command()])
    sleeps = []
    make(cloud, sleeps).poll(DEVICES)
    make(cloud, sleeps).poll(DEVICES)  # a restarted agent, same data_dir
    assert sleeps == [2]
    assert len(cloud.posts) == 1
    assert caplog.text.count("already handled cmd_51d0") == 2


def test_result_is_retried_after_network_failure_without_rerunning(make, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="signalbloom_agent")
    cloud = Cloud([command()], result_replies=[requests.ConnectionError("down"), 200])
    sleeps = []
    poller = make(cloud, sleeps)
    poller.poll(DEVICES)

    assert poller.waiting == 1
    assert state(tmp_path)["cmd_51d0"]["state"] == "reporting"
    assert state(tmp_path)["cmd_51d0"]["result"]["status"] == "succeeded"
    assert "cloud unreachable while checking for fixes, 1 fix result waiting" in caplog.text

    make(cloud, sleeps).poll(DEVICES)  # next cycle, even after a restart
    assert sleeps == [2]  # the fix ran once
    assert [p[0] for p in cloud.posts] == [LIST_URL + "/cmd_51d0/result"] * 2
    assert cloud.posts[0][1] == cloud.posts[1][1]  # the same saved result
    assert state(tmp_path) == {"cmd_51d0": {"state": "done"}}


def test_a_failed_report_does_not_skip_the_rest_of_the_batch(make, tmp_path):
    cloud = Cloud([command("cmd_a"), command("cmd_b")],
                  result_replies=[requests.ConnectionError("down")])
    sleeps = []
    make(cloud, sleeps).poll(DEVICES)
    assert sleeps == [2, 2]  # both ran; the cloud won't send them again
    assert {k: v["state"] for k, v in state(tmp_path).items()} == {
        "cmd_a": "reporting", "cmd_b": "reporting"}


def test_no_new_commands_are_fetched_while_results_cant_be_sent(make):
    cloud = Cloud([command("cmd_a")], [command("cmd_b")],
                  result_replies=[requests.ConnectionError("down"),
                                  requests.ConnectionError("down")])
    poller = make(cloud)
    poller.poll(DEVICES)
    poller.poll(DEVICES)
    assert len(cloud.gets) == 1  # cmd_b stays with the cloud until we're back


def test_409_means_already_recorded(make, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="signalbloom_agent")
    cloud = Cloud([command()], result_replies=[409])
    poller = make(cloud)
    poller.poll(DEVICES)
    poller.poll(DEVICES)
    assert len(cloud.posts) == 1
    assert state(tmp_path) == {"cmd_51d0": {"state": "done"}}
    assert "result for cmd_51d0 was already recorded" in caplog.text
    assert not (tmp_path / REJECTED_FILE).exists()


def test_404_drops_and_logs(make, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="signalbloom_agent")
    cloud = Cloud([command()], result_replies=[404])
    poller = make(cloud)
    poller.poll(DEVICES)
    poller.poll(DEVICES)
    assert len(cloud.posts) == 1
    assert state(tmp_path) == {"cmd_51d0": {"state": "done"}}
    assert "cmd_51d0" in caplog.text and "HTTP 404: says 404" in caplog.text


def test_5xx_keeps_the_result_for_next_cycle(make, tmp_path):
    cloud = Cloud([command()], result_replies=[503, 200])
    sleeps = []
    poller = make(cloud, sleeps)
    poller.poll(DEVICES)
    assert poller.waiting == 1
    poller.poll(DEVICES)
    assert poller.waiting == 0 and sleeps == [2] and len(cloud.posts) == 2


@pytest.mark.parametrize("status", [401, 403])
def test_auth_error_on_list(make, status):
    cloud = Cloud(get_reply=response(status))
    with pytest.raises(AuthError) as err:
        make(cloud).poll(DEVICES)
    assert f"HTTP {status}" in str(err.value) and "setup code" in str(err.value)
    assert TOKEN not in str(err.value)


@pytest.mark.parametrize("status", [401, 403])
def test_auth_error_on_result_keeps_it(make, tmp_path, status):
    cloud = Cloud([command()], result_replies=[status])
    with pytest.raises(AuthError):
        make(cloud).poll(DEVICES)
    assert state(tmp_path)["cmd_51d0"]["state"] == "reporting"


def test_stopped_mid_fix_is_reported_as_failed_not_rerun(make, tmp_path):
    (tmp_path / STATE_FILE).write_text(json.dumps(
        {"commands": {"cmd_51d0": {"state": "running"}}}), encoding="utf-8")
    cloud = Cloud([command()])  # even if the cloud sends it again
    sleeps = []
    make(cloud, sleeps).poll(DEVICES)
    assert sleeps == []
    assert cloud.reported() == [("cmd_51d0", "failed",
                                 "Agent stopped before the simulated fix finished.")]


def test_list_errors_are_logged_not_raised(make, caplog):
    caplog.set_level(logging.INFO, logger="signalbloom_agent")
    make(Cloud(get_reply=requests.ConnectionError("down"))).poll(DEVICES)
    make(Cloud(get_reply=response(503))).poll(DEVICES)
    make(Cloud(get_reply=response(200, {"commands": "nope"}))).poll(DEVICES)
    make(Cloud(["junk", {"type": "restart_device"}])).poll(DEVICES)
    assert "cloud unreachable while checking for fixes" in caplog.text
    assert "cloud error while checking for fixes (HTTP 503)" in caplog.text
    assert "fix list the agent couldn't read" in caplog.text
    assert caplog.text.count("skipped a fix with no id") == 2


def test_unreadable_state_file_starts_empty(make, tmp_path, caplog):
    (tmp_path / STATE_FILE).write_text("{not json", encoding="utf-8")
    make(Cloud()).poll(DEVICES)
    assert "could not read" in caplog.text


def test_repr_hides_token(make):
    assert TOKEN not in repr(make(Cloud()))


@pytest.mark.parametrize("status, action", [
    (200, "recorded"), (201, "recorded"), (409, "recorded"),
    (400, "drop"), (404, "drop"), (422, "drop"),
    (408, "retry"), (429, "retry"), (500, "retry"), (503, "retry"),
])
def test_result_action(status, action):
    assert result_action(status) == action


def test_400_result_is_dropped_and_saved_to_rejected(make, tmp_path):
    cloud = Cloud([command()], result_replies=[400])
    poller = make(cloud)
    poller.poll(DEVICES)
    poller.poll(DEVICES)
    assert len(cloud.posts) == 1
    (line,) = (tmp_path / REJECTED_FILE).read_text(encoding="utf-8").splitlines()
    record = json.loads(line)
    assert record["command_id"] == "cmd_51d0" and record["status"] == 400
    assert record["result"]["status"] == "succeeded"


def test_poll_counts_fixes_that_succeeded(make):
    cloud = Cloud([command("cmd_1"), command("cmd_2", kind="wipe_disk"),
                   command("cmd_3", mac="aa:bb:cc:dd:ee:01")], [command("cmd_1")])
    poller = make(cloud)
    assert poller.poll(DEVICES) == 2  # cmd_2 refused
    assert poller.poll(DEVICES) == 0  # cmd_1 again: skipped, not re-run


def test_poll_still_counts_successes_when_reporting_fails(make):
    cloud = Cloud([command()], result_replies=[requests.ConnectionError()])
    assert make(cloud).poll(DEVICES) == 1
