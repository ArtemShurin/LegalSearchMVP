"""
Prometheus-метрики для ML-вызовов (эмбеддинг здесь не трогаем — он быстрый
и не был источником проблем, фокус на LLM, см. memory:
project-backend-production-readiness, пункт 5). Общий модуль, не внутри
backend/, т.к. ml/generation.py используется и backend'ом, и потенциально
скриптами индексации.
"""

from prometheus_client import Histogram, Gauge

llm_request_duration_seconds = Histogram(
    "llm_request_duration_seconds",
    "Длительность одного вызова Ollama (без времени ожидания в очереди)",
    ["call_type"],  # rewrite | answer
)

llm_queue_length = Gauge(
    "llm_queue_length",
    "Сколько запросов сейчас реально ждут своей очереди на LLM (не считая того, что уже выполняется)",
)
