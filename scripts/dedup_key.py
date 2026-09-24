"""
Единая нормализация ключа дедупликации НПА между RusLawOD и pravo.gov.ru.

Внутренние ID документов у источников НЕСОВМЕСТИМЫ:
  - RusLawOD: pravogovruNd — 9-значный ID старой системы pravo.gov.ru/proxy/ips
  - publication.pravo.gov.ru: 16-значный ID (0001ГГГГММДД0000) новой системы

Прямого маппинга между ними на сайте нет. Вместо внутреннего ID используем
официальные реквизиты акта — тип + номер + дата — единственное, что реально
совпадает между источниками (это то, как документ цитируется юридически).
"""
import re


def normalize_number(number: str) -> str:
    return re.sub(r"\s+", "", (number or "")).upper()


def make_dedup_key(doc_type: str, number: str, date: str) -> str | None:
    """
    doc_type: полное название типа акта ("Указ Президента...", "Постановление...")
    number:   официальный номер акта ("674", "3395-8 ГД")
    date:     дата в формате ДД.ММ.ГГГГ

    Возвращает None, если номер или дата не распознаны — ТАКОЙ ключ нельзя
    использовать для дедупликации: у разных документов с непарсенными
    реквизитами он совпадёт ("тип||"), и они ошибочно пометятся как дубли
    друг друга. None явно сигналит "не знаем — не сравнивать".
    """
    number_norm = normalize_number(number)
    date_norm = (date or "").strip()
    if not number_norm or not date_norm:
        return None
    type_word = (doc_type or "").strip().split(" ")[0].lower()
    return f"{type_word}|{number_norm}|{date_norm}"
