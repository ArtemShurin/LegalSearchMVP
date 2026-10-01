export type User = {
  id: string;
  email: string;
  role: string;
  company_id: string | null;
};

export type SearchFilters = {
  court?: string | null;
  region?: string | null;
  date_from?: string | null;
  date_to?: string | null;
  category?: string | null;
};

export type SourceItem = {
  document_id: string;
  title: string;
  relevance: number;
  fragment: string;
};

export type HistorySource = {
  document_id: string;
  title: string;
  relevance: number | null;
  fragment: string | null;
};

export type HistoryEntry = {
  id: string;
  text: string;
  answer: string;
  created_at: string;
  sources: HistorySource[];
};

export type CollectionItemResponse = {
  id: string;
  document_id: string;
  title: string | null;
  fragment: string | null;
  relevance: number | null;
  added_at: string;
};

export type CollectionResponse = {
  id: string;
  name: string;
  created_at: string;
  items: CollectionItemResponse[];
};

export type DocumentStatus = "processing" | "indexed" | "failed";

export type DocumentResponse = {
  id: string;
  filename: string;
  file_size: number;
  status: DocumentStatus;
  error_message: string | null;
  created_at: string;
};
