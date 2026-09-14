"""Logging configuration — Global pretty-printing of dict and list objects for console logs."""

import copy
import json
import logging
import sys
from typing import Any

NOISY_LOGGERS = [
    "google",
    "google_adk",
    "google.adk",
    "google.adk.runners",
    "google.adk.models",
    "google.adk.tools",
    "google.genai",
    "google_llm",
    "runners",
    "models",
    "httpx",
    "httpcore",
    "urllib3",
]


class PrettyJsonFormatter(logging.Formatter):
    """Custom logging formatter that automatically pretty-prints dict and list arguments as JSON."""

    @staticmethod
    def _pretty_format(obj: Any) -> Any:
        try:
            return "\n" + json.dumps(obj, indent=2, ensure_ascii=False, default=str)
        except Exception:
            return str(obj)

    def format(self, record: logging.LogRecord) -> str:
        # Shallow copy record to avoid mutating the original record for other handlers
        record_copy = copy.copy(record)

        if isinstance(record_copy.msg, (dict, list)):
            record_copy.msg = self._pretty_format(record_copy.msg)
        elif record_copy.args:
            if isinstance(record_copy.args, dict):
                # Check if msg uses named placeholders like %(key)s
                msg_str = str(record_copy.msg)
                has_named_format = "%(" in msg_str
                if not has_named_format:
                    # Single dict argument passed for positional formatting (unwrapped by LogRecord.__init__)
                    record_copy.args = (self._pretty_format(record_copy.args),)
                else:
                    new_dict = {}
                    for k, v in record_copy.args.items():
                        if isinstance(v, (dict, list)):
                            new_dict[k] = self._pretty_format(v)
                        else:
                            new_dict[k] = v
                    record_copy.args = new_dict
            elif isinstance(record_copy.args, tuple):
                new_tuple = []
                for arg in record_copy.args:
                    if isinstance(arg, (dict, list)):
                        new_tuple.append(self._pretty_format(arg))
                    else:
                        new_tuple.append(arg)
                record_copy.args = tuple(new_tuple)

        return super().format(record_copy)


def configure_logging(level: int = logging.INFO) -> None:
    """Configure console logging format and suppress redundant third-party logs."""
    formatter = PrettyJsonFormatter(
        "%(asctime)s - %(levelname)s - %(name)s:%(lineno)d - %(message)s"
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Configure existing handlers or add a StreamHandler if none exists
    if not root_logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)
    else:
        for handler in root_logger.handlers:
            handler.setFormatter(formatter)

    # Suppress verbose noise from external frameworks/SDKs
    for logger_name in NOISY_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.WARNING)
