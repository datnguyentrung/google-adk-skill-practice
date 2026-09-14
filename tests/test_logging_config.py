"""Tests for PrettyJsonFormatter and global logging configuration."""

import logging
from app.core.logging_config import PrettyJsonFormatter, configure_logging


def test_pretty_json_formatter_dict_msg():
    formatter = PrettyJsonFormatter("%(message)s")
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg={"key": "value", "num": 123},
        args=(),
        exc_info=None,
    )
    formatted = formatter.format(record)
    assert '{\n  "key": "value",\n  "num": 123\n}' in formatted


def test_pretty_json_formatter_list_msg():
    formatter = PrettyJsonFormatter("%(message)s")
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg=["item1", "item2"],
        args=(),
        exc_info=None,
    )
    formatted = formatter.format(record)
    assert '[\n  "item1",\n  "item2"\n]' in formatted


def test_pretty_json_formatter_dict_in_args():
    formatter = PrettyJsonFormatter("%(message)s")
    data = {"status": "ok", "items": [1, 2]}
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="Data received: %s",
        args=(data,),
        exc_info=None,
    )
    formatted = formatter.format(record)
    assert 'Data received: \n{\n  "status": "ok",\n  "items": [\n    1,\n    2\n  ]\n}' in formatted


def test_pretty_json_formatter_dict_named_format():
    formatter = PrettyJsonFormatter("%(message)s")
    data = {"user": "alice", "profile": {"age": 30, "role": "admin"}}
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="User %(user)s profile: %(profile)s",
        args=data,
        exc_info=None,
    )
    formatted = formatter.format(record)
    assert 'User alice profile: \n{\n  "age": 30,\n  "role": "admin"\n}' in formatted


def test_pretty_json_formatter_normal_str():
    formatter = PrettyJsonFormatter("%(message)s")
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="Simple log message: %s",
        args=("hello",),
        exc_info=None,
    )
    formatted = formatter.format(record)
    assert formatted == "Simple log message: hello"


def test_pretty_json_formatter_does_not_mutate_original_record():
    formatter = PrettyJsonFormatter("%(message)s")
    original_dict = {"a": 1}
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="Data: %s",
        args=(original_dict,),
        exc_info=None,
    )
    _ = formatter.format(record)
    # Original record args must remain untouched (LogRecord unwraps 1-element dict args to original_dict)
    assert record.args is original_dict or record.args == (original_dict,)


def test_configure_logging_sets_pretty_json_formatter():
    configure_logging(logging.DEBUG)
    root = logging.getLogger()
    assert root.level == logging.DEBUG
    assert len(root.handlers) > 0
    assert isinstance(root.handlers[0].formatter, PrettyJsonFormatter)
