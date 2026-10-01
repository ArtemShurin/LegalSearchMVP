from datetime import datetime
from pydantic import BaseModel, Field, field_validator


class CollectionCreate(BaseModel):
    name: str = Field(max_length=255)


class CollectionRename(BaseModel):
    name: str = Field(max_length=255)


class CollectionItemAdd(BaseModel):
    document_id: str = Field(max_length=255)
    title: str = Field(max_length=500)
    fragment: str | None = Field(default=None, max_length=2100)
    relevance: float | None = None


class CollectionItemResponse(BaseModel):
    id: str
    document_id: str
    title: str | None
    fragment: str | None
    relevance: float | None
    added_at: datetime

    @field_validator("id", mode="before")
    @classmethod
    def _stringify_uuid(cls, v):
        return str(v) if v is not None else v

    class Config:
        from_attributes = True


class CollectionResponse(BaseModel):
    id: str
    name: str
    created_at: datetime
    items: list[CollectionItemResponse] = []

    @field_validator("id", mode="before")
    @classmethod
    def _stringify_uuid(cls, v):
        return str(v) if v is not None else v

    class Config:
        from_attributes = True

