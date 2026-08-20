from typing import Literal

from pydantic import BaseModel


class NodeIdentity(BaseModel):
    class_name: str

    strategy: Literal[
        "natural_key",
        "unresolved",
    ]

    key_name: str | None = None
    key_value: str | None = None

    reason: str | None = None
