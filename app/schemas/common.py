from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """All API payloads are camelCase on the wire, snake_case in Python."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )


class ErrorDetail(CamelModel):
    code: str
    message: str
    details: dict | None = None


class ErrorResponse(CamelModel):
    error: ErrorDetail
