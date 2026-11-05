"""
Метрики уровня backend (в отличие от ml/metrics.py — те про сами вызовы LLM,
эти про исходы запросов и ошибки БД). См. memory:
project-backend-production-readiness, пункт 5.
"""

from prometheus_client import Counter

search_requests_total = Counter(
    "search_requests_total",
    "Итоги запросов к /search/ по исходу",
    ["outcome"],  # answered | no_sources | llm_unavailable | rate_limited | cjk_aborted
)

db_errors_total = Counter(
    "db_errors_total",
    "Исключения, всплывшие во время открытой сессии БД (приблизительно — не "
    "каждое такое исключение обязательно из-за самой БД, но практически "
    "большинство реальных случаев — именно из-за неё)",
)
