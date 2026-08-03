"""Base model shared by navigation schemas."""

from pydantic import BaseModel, ConfigDict


class SchemaBaseModel(BaseModel):
    """Base model for graph/navigation schemas."""

    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",
        str_strip_whitespace=True,
    )


__all__ = ["SchemaBaseModel"]
