import logging
import sys

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


def configure_logging(level: int = logging.INFO) -> None:
    """Configure console logging format and suppress redundant third-party logs."""
    formatter = logging.Formatter(
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
