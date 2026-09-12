from pydantic import BaseModel, Field


class VitalCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=64)
    unit: str | None = Field(default=None, max_length=32)
