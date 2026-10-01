from datetime import datetime
from pydantic import BaseModel, field_validator


class DocumentResponse(BaseModel):
    id: str
    filename: str
    file_size: int
    status: str
    error_message: str | None
    created_at: datetime

    @field_validator("id", mode="before")
    @classmethod
    def _stringify_uuid(cls, v):
        return str(v) if v is not None else v

    class Config:
        from_attributes = True
