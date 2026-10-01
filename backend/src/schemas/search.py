from pydantic import BaseModel


class SearchFilters(BaseModel):
    court: str | None = None
    region: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    category: str | None = None


class SearchRequest(BaseModel):
    query: str
    filters: SearchFilters | None = None
    internal_only: bool = False  # искать только среди документов своей компании, без публичного корпуса


class SourceItem(BaseModel):
    document_id: str
    title: str
    relevance: float
    fragment: str

