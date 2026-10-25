import logging
import threading

import pytest
import yaml

from signalbloom_agent.agent import run
from signalbloom_agent.buffer import ReadingBuffer
from signalbloom_agent.config import ConfigError, load_config
from signalbloom_agent.demo import DEMO_FAULT_FILE, DemoFault, apply_fault
from signalbloom_agent.sender import SendSummary

from test_agent import FakeClock, FakeDevices, FakePoller, RecordingSender, make_config
from test_config import EXAMPLE, write_config


def reading(latency=12.4, loss=0, up=True) -> dict:
    return {"measured_at": "2026-10-23T10:00:00+01:00", "latency_ms": latency,
            "packet_loss_pct": loss, "internet_up": up, "download_mbps": 87.3,
            "upload_mbps": None, "error_rate_pct": None}


# --- apply_fault -----------------------------------------------------------

def test_fault_adds_latency_and_loss_and_keeps_the_rest():
    original = reading()
    changed = apply_fault(original, 300, 20)
    assert changed["latency_ms"] == 312.4
    assert changed["packet_loss_pct"] == 20
    assert changed["internet_up"] is True
    assert {k: v for k, v in changed.items() if k not in ("latency_ms", "packet_loss_pct")} \
        == {k: v for k, v in original.items() if k not in ("latency_ms", "packet_loss_pct")}
    assert original == reading()  # input not changed
    assert set(changed) == set(original)  # no demo flag added


@pytest.mark.parametrize("loss, expected", [(90, 100), (80, 100), (100, 100), (79, 99)])
def test_loss_is_capped_at_100(loss, expected):
    assert apply_fault(reading(loss=loss), 300, 20)["packet_loss_pct"] == expected


def test_no_reply_latency_stays_none_and_internet_up_kept():
    changed = apply_fault(reading(latency=None, loss=100, up=False), 300, 20)
    assert changed["latency_ms"] is None
    assert changed["packet_loss_pct"] == 100
    assert changed["internet_up"] is False


# --- DemoFault ---------------------------------------------------------------

def test_off_by_default_leaves_readings_unchanged(tmp_path):
    demo = DemoFault(False, tmp_path, 300, 20)
    original = reading()
    assert demo.process(original) == reading()
    assert original == reading()


def test_flag_turns_fault_on(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="signalbloom_agent")
    demo = DemoFault(True, tmp_path, 300, 20)
    assert demo.process(reading())["latency_ms"] == 312.4
    assert demo.process(reading())["packet_loss_pct"] == 20
    assert caplog.text.count("DEMO FAULT ON: readings are being changed") == 1


def test_file_turns_fault_on_and_off_without_restart(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="signalbloom_agent")
    demo = DemoFault(False, tmp_path, 300, 20)
    assert demo.process(reading()) == reading()

    (tmp_path / DEMO_FAULT_FILE).touch()
    assert demo.process(reading())["latency_ms"] == 312.4
    assert demo.process(reading())["latency_ms"] == 312.4

    (tmp_path / DEMO_FAULT_FILE).unlink()
    assert demo.process(reading()) == reading()  # back to normal
    assert caplog.text.count("DEMO FAULT ON") == 1
    assert caplog.text.count("DEMO FAULT OFF") == 1


# --- in the main loop --------------------------------------------------------

def test_run_changes_readings_only_while_file_exists(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="signalbloom_agent")
    clock = FakeClock()
    stop = threading.Event()
    buffer = ReadingBuffer(tmp_path, 100)
    sender = RecordingSender(buffer)
    sender.clock = clock
    fault_file = tmp_path / DEMO_FAULT_FILE
    calls = []

    def measure():
        calls.append(1)
        if len(calls) == 2:
            fault_file.touch()
        if len(calls) == 3:
            fault_file.unlink()
            stop.set()
        r = reading()
        r.pop("download_mbps")
        return r

    bandwidth = type("B", (), {"running": False, "take_result": lambda self: 87.3,
                               "start_if_due": lambda self: False})()
    run(make_config(tmp_path), stop=stop, clock=clock, sleep=clock.sleep, measure=measure,
        buffer=buffer, sender=sender, devices=FakeDevices(), bandwidth=bandwidth,
        poller=FakePoller())

    sent = [f[1][0] for f in sender.flushes]
    assert [r["latency_ms"] for r in sent] == [12.4, 312.4, 12.4]
    assert [r["packet_loss_pct"] for r in sent] == [0, 20, 0]
    assert all(set(r) == set(reading()) for r in sent)  # no demo flag sent
    assert "DEMO FAULT ON" in caplog.text and "DEMO FAULT OFF" in caplog.text
    assert "(latency 312.4 ms, loss 20%)" in caplog.text


def test_send_line_shows_latency_and_loss():
    summary = SendSummary(accepted=1, requests=1, latest=reading())
    assert summary.lines() == ["sent 1 reading, 0 duplicates (latency 12.4 ms, loss 0%)"]
    summary.latest = reading(latency=None, loss=100)
    assert "(latency no reply, loss 100%)" in summary.lines()[0]


# --- config ------------------------------------------------------------------

def test_demo_amounts_from_example():
    config = load_config(EXAMPLE)
    assert config.demo_fault_latency_ms == 300
    assert config.demo_fault_loss_pct == 20


def test_demo_amounts_default_when_missing_and_zero_kept(tmp_path):
    config = load_config(write_config(tmp_path, demo_fault_latency_ms=None,
                                      demo_fault_loss_pct=None))
    assert (config.demo_fault_latency_ms, config.demo_fault_loss_pct) == (300, 20)
    config = load_config(write_config(tmp_path, demo_fault_latency_ms=0, demo_fault_loss_pct=0))
    assert (config.demo_fault_latency_ms, config.demo_fault_loss_pct) == (0, 0)


@pytest.mark.parametrize("changes, expected", [
    ({"demo_fault_latency_ms": -1}, "'demo_fault_latency_ms' must be a whole number"),
    ({"demo_fault_loss_pct": "lots"}, "'demo_fault_loss_pct' must be a whole number"),
    ({"demo_fault_loss_pct": 101}, "'demo_fault_loss_pct' must be 100 or less"),
])
def test_bad_demo_amounts_rejected(tmp_path, changes, expected):
    with pytest.raises(ConfigError, match=expected):
        load_config(write_config(tmp_path, **changes))


# --- cleared by an approved fix ---------------------------------------------

def test_fix_clears_file_started_fault(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="signalbloom_agent")
    (tmp_path / DEMO_FAULT_FILE).touch()
    demo = DemoFault(False, tmp_path, 300, 20)
    assert demo.process(reading())["latency_ms"] == 312.4

    demo.clear_by_fix()
    assert not (tmp_path / DEMO_FAULT_FILE).exists()
    assert caplog.text.count("DEMO FAULT OFF: cleared by approved fix") == 1
    assert demo.process(reading()) == reading()
    assert caplog.text.count("DEMO FAULT OFF") == 1  # no second OFF line next cycle


def test_fix_clears_flag_started_fault_for_rest_of_run(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="signalbloom_agent")
    demo = DemoFault(True, tmp_path, 300, 20)
    assert demo.process(reading())["latency_ms"] == 312.4

    demo.clear_by_fix()
    assert "DEMO FAULT OFF: cleared by approved fix" in caplog.text
    for _ in range(3):
        assert demo.process(reading()) == reading()
    assert not demo.active()


def test_fix_does_nothing_when_fault_is_off(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="signalbloom_agent")
    demo = DemoFault(False, tmp_path, 300, 20)
    demo.process(reading())
    demo.clear_by_fix()
    assert "DEMO FAULT" not in caplog.text


def test_run_clears_fault_after_a_successful_fix(tmp_path):
    clock = FakeClock()
    stop = threading.Event()
    buffer = ReadingBuffer(tmp_path, 100)
    sender = RecordingSender(buffer)
    sender.clock = clock
    (tmp_path / DEMO_FAULT_FILE).touch()
    calls = []

    def measure():
        calls.append(1)
        if len(calls) == 3:
            stop.set()
        r = reading()
        r.pop("download_mbps")
        return r

    class OneFixPoller(FakePoller):
        def poll(self, devices):
            super().poll(devices)
            return 1 if len(self.polls) == 2 else 0  # fix succeeds on cycle 2

    bandwidth = type("B", (), {"running": False, "take_result": lambda self: None,
                               "start_if_due": lambda self: False})()
    run(make_config(tmp_path), stop=stop, clock=clock, sleep=clock.sleep, measure=measure,
        buffer=buffer, sender=sender, devices=FakeDevices(), bandwidth=bandwidth,
        poller=OneFixPoller(), demo_fault=True)

    assert [f[1][0]["latency_ms"] for f in sender.flushes] == [312.4, 312.4, 12.4]
    assert not (tmp_path / DEMO_FAULT_FILE).exists()
