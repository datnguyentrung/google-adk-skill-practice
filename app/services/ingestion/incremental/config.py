import os


def _env_flag(name: str, default: bool = False) -> bool:
    raw_value = os.getenv(name)

    if raw_value is None:
        return default

    return raw_value.strip().casefold() in {"1", "true", "yes", "on"}


def persistent_staging_enabled() -> bool:
    """
    Kiểm tra incremental persistent staging V2 có được bật hay không.

    Returns:
        True nếu flow ingestion V2 được bật.
    """
    return _env_flag(
        "INGESTION_PERSISTENT_STAGING_ENABLED",
        default=False,
    )


__all__ = [
    "persistent_staging_enabled",
]
