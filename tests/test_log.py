"""Structured logging tests (#24)."""

import json
import logging

import pytest

from remote_desktop import logging as rd_log


@pytest.fixture
def sink(tmp_path, monkeypatch):
    path = tmp_path / "control.log"
    rd_log._configured.clear()
    yield path
    rd_log._logger.handlers.clear()
    rd_log._configured.clear()


def test_log_line_has_time_level_rid_and_message(sink):
    rd_log.setup(level="INFO", fmt="text", log_file=str(sink), console=False)
    token = rd_log.request_id.set("abc123")
    try:
        rd_log.log("hello world")
    finally:
        rd_log.request_id.reset(token)
    line = sink.read_text().strip()
    assert "INFO [abc123] hello world" in line
    assert line[:8].count(":") == 2  # HH:MM:SS timestamp kept


def test_log_outside_request_has_dash_rid(sink):
    rd_log.setup(level="INFO", fmt="text", log_file=str(sink), console=False)
    rd_log.log("startup message")
    assert "INFO [-] startup message" in sink.read_text()


def test_log_fields_appended(sink):
    rd_log.setup(level="INFO", fmt="text", log_file=str(sink), console=False)
    rd_log.log("ws proxy close", peer="1.2.3.4")
    assert "ws proxy close peer=1.2.3.4" in sink.read_text()


def test_json_format_is_parseable(sink):
    rd_log.setup(level="INFO", fmt="json", log_file=str(sink), console=False)
    token = rd_log.request_id.set("deadbeef")
    try:
        rd_log.log("structured", extra_field=1)
    finally:
        rd_log.request_id.reset(token)
    rec = json.loads(sink.read_text().strip())
    assert rec["level"] == "info"
    assert rec["rid"] == "deadbeef"
    assert rec["msg"] == "structured extra_field=1"


def test_level_filtering(sink):
    rd_log.setup(level="WARNING", fmt="text", log_file=str(sink), console=False)
    rd_log.log("quiet info")
    assert "quiet info" not in sink.read_text()
    rd_log.warning("loud warning")
    assert "loud warning" in sink.read_text()


def test_setup_is_idempotent(sink):
    rd_log.setup(level="INFO", fmt="text", log_file=str(sink), console=False)
    rd_log.setup(level="INFO", fmt="text", log_file=str(sink), console=False)
    rd_log.log("once")
    assert sink.read_text().count("once") == 1


def test_log_file_tail(sink):
    rd_log.setup(level="INFO", fmt="text", log_file=str(sink), console=False)
    rd_log.log("line-one")
    rd_log.log("line-two")
    tail = rd_log.log_file_tail(log_file=str(sink))
    assert "line-two" in tail and "line-one" in tail


def test_default_level_is_info(sink, monkeypatch):
    rd_log.setup(level=None, fmt=None, log_file=str(sink), console=False)
    assert rd_log._logger.level == logging.INFO


def test_unwritable_sink_does_not_raise(tmp_path):
    blocked = tmp_path / "dir"  # file handler needs a file path
    blocked.mkdir()
    rd_log.setup(level="INFO", fmt="text", log_file=str(blocked), console=False)
    rd_log.log("must not raise")
    rd_log._logger.handlers.clear()
    rd_log._configured.clear()
