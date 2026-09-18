from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, Field

DataT = TypeVar("DataT")


class SuccessResponse(BaseModel, Generic[DataT]):
    success: Literal[True] = True
    data: DataT
    meta: dict[str, Any] = Field(default_factory=dict)
