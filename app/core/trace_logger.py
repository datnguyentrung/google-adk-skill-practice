"""Central trace logger writing formatted pprint outputs to logs/log.txt and console."""

import sys
from pathlib import Path
from pprint import pformat
from typing import Any

LOG_FILE = Path(__file__).resolve().parent.parent.parent / "logs" / "log.txt"


def write_trace(text: str) -> None:
    """Write text to logs/log.txt and stdout safely."""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        pass

    try:
        print(text)
    except Exception:
        try:
            encoding = getattr(sys.stdout, "encoding", "utf-8") or "utf-8"
            print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))
        except Exception:
            pass


def pprint(obj: Any, *args: Any, indent: int = 2, sort_dicts: bool = False, **kwargs: Any) -> None:
    """Pretty-print object to both logs/log.txt and console."""
    if isinstance(obj, str):
        write_trace(obj)
    else:
        formatted = pformat(obj, indent=indent, sort_dicts=sort_dicts, **kwargs)
        write_trace(formatted)


def trace_pprint(header: str, obj: Any = None, **kwargs: Any) -> None:
    """Print header followed by pretty-printed object to logs/log.txt and console."""
    write_trace(f"\n{header}")
    if obj is not None:
        if isinstance(obj, str):
            write_trace(obj)
        else:
            formatted = pformat(obj, indent=2, sort_dicts=False, **kwargs)
            write_trace(formatted)
