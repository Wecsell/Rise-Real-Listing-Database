"""
Роутинг "пустое поле карточки -> какой документ его закроет" (implementation_plan.md, Э2).

Смысл этапа в одной фразе из плана: не читать материалы, которые не нужны для
пустых полей. Открытие документа - самая дорогая операция пайплайна (страница
PDF уходит в модель как изображение), листинг и разбор имени - бесплатны.
Поэтому решение "открывать/не открывать" принимается ДО открытия, по имени файла.

Три уровня стоимости (план):
  листинг папки        - HTTP, без модели, всегда
  роутинг по имени     - регулярка, всегда          <- этот модуль
  открытие и разбор    - дорого, только под пустое поле

Модуль сознательно ничего не открывает и не ходит в сеть: он только отвечает
"какие файлы стоит открыть и ради каких полей". Само открытие - следующий шаг.
"""
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Set

logger = logging.getLogger("DocRouter")

# Бюджет открытий на проект за один проход (план: "N в конфиг, старт - 5").
DEFAULT_OPEN_BUDGET = 5

# Карта из плана, Э2. Ключ - тип документа, значение - какие поля он закрывает.
# Индонезийские аббревиатуры важнее английских слов: документы застройщиков
# называются на bahasa, и именно по ним файл опознаётся однозначно.
#
# ПОРЯДОК ЗНАЧИМ - выигрывает первое совпадение, поэтому специфичное идёт раньше
# общего. Два реальных конфликта на папке Four Palms, найденные живым прогоном:
#   "PT Seratus MPI - AKTA PENDIRIAN-6.pdf" - общий \bakta\b (lease) перебивал
#     специфичный "akta pendirian" (company), и учредительный акт компании
#     уезжал в срок аренды;
#   там почти каждый файл назван "PT <компания> - <что это>", то есть слабый
#     признак \bpt[\s.] совпадает вообще со всем - он обязан проверяться
#     последним, уже после настоящего типа документа.
DOC_TYPE_PATTERNS = [
    ("permits", r"\bpbg\b|\bslf\b|\bimb\b|simbg"),
    ("zoning", r"\bitr\b|\bpkkpr\b|tata\s*ruang|zoning|\bkkpr\b"),
    ("company", r"akta\s*pendirian|\bnib\b|\bnpwp\b|notaris"),
    ("lease", r"\bsmt\b|sewa|lease|\bakta\b|perjanjian"),
    ("pricing", r"pric(?:e|ing)|harga|pricelist|specification|spesifikasi|quotation"),
    ("presentation", r"presentation|brochure|devkit|catalog|каталог|презентация|deck|info|about|overview|concept|masterplan|booklet"),
    ("location", r"location|map|карта|локация|address|адрес|район|district"),
    ("certificate", r"\bshm\b|\bshgb\b|sertifikat|certificate\s*of\s*land"),
    ("company", r"company|\bpt[\s.]"),
]

# Какие поля Airtable закрывает документ каждого типа. Имена полей - ровно те,
# что в app/gaps.py: роутер должен говорить на языке детектора пробелов, иначе
# сопоставление "пустое поле -> документ" придётся делать вручную в двух местах.
DOC_TYPE_TO_FIELDS: Dict[str, Set[str]] = {
    "lease": {"Lease Term (years)", "Ownership Type"},
    "zoning": {"Land Zoning Color", "District"},
    "permits": {"Handover Permits"},
    "company": {"Developer"},
    "pricing": {"Price From (USD)", "Price from(USD)", "Area from (m2)", "Handover Date", "Construction stage"},
    "presentation": {"District", "Property Type", "Construction stage", "Handover Date", "Location Link", "Ownership Type"},
    "location": {"District", "Location Link"},
    "certificate": {"Land Zoning Color"},
}

_COMPILED = [(name, re.compile(pattern, re.IGNORECASE)) for name, pattern in DOC_TYPE_PATTERNS]

# Skip-лист (план, Э1): паспорта и удостоверения не скачиваем, не парсим и не
# прикладываем в Airtable. Нулевая польза для полей карточки при дорогих токенах,
# плюс это персональные данные третьих лиц - в Legal-папке Four Palms реально
# лежали KITAP/паспорта директоров. Тот же список, что в app/drive_mirror.py,
# но здесь он про "не открывать", а там про "не копировать".
_SKIP_NAME_RE = re.compile(
    r"passport|pasport|\bktp\b|kitap|\bid[\s_-]?card\b|паспорт|удостоверен",
    re.IGNORECASE,
)

# Рендеры, фото и видео по плану не парсим вообще: они нужны ссылкой на лендинге,
# а не как источник полей, и они же самые дорогие в токенах.
_MEDIA_MIME_PREFIXES = ("image/", "video/", "audio/")


def is_skipped_document(name: Optional[str], path: str = "") -> bool:
    """Документ личности: не открывать ни при каких пустых полях."""
    return bool(_SKIP_NAME_RE.search(f"{path or ''}/{name or ''}"))


def classify_document(name: Optional[str], path: str = "") -> Optional[str]:
    """
    Тип документа по имени файла (и папкам на его пути - у застройщиков
    осмысленное имя часто на папке: "03 Legal and Due Diligence/scan_01.pdf").

    None означает "по имени не опознан" - такой файл кандидат на дорогой
    fallback-разбор, а не мусор.
    """
    haystack = f"{path or ''}/{name or ''}"
    for doc_type, pattern in _COMPILED:
        if pattern.search(haystack):
            return doc_type
    return None


def fields_closed_by(doc_type: Optional[str]) -> Set[str]:
    return set(DOC_TYPE_TO_FIELDS.get(doc_type or "", set()))


def is_document_scan(file_info: Dict[str, Any]) -> bool:
    """
    Изображение, которое на самом деле скан/скриншот документа, а не рендер.

    Отсекать все картинки нельзя: на живой Legal-папке Four Palms единственное
    свидетельство разрешений - "SLF reg.jpeg", скриншот из SIMBG. План его
    разбор прямо предусматривает (тест 12 про статус Perbaikan Dokumen), то есть
    правило "фото не парсим" написано про рендеры и фотографии объекта, а не про
    сканы документов.

    Отличаем по имени: скан документа опознаётся теми же шаблонами, что и PDF
    (PBG/SLF/ITR/SMT...), а рендер называется "3.1.jpg" и не совпадает ни с чем.
    """
    mime = file_info.get("mimeType") or ""
    if not mime.startswith("image/"):
        return False
    return classify_document(file_info.get("name"), file_info.get("path", "")) is not None


def needs_vision(file_info: Dict[str, Any]) -> bool:
    """
    Файл придётся отдавать зрению, а не текстовому слою.

    План: "Текстовый слой раньше зрения" - у PDF сначала пробуем pypdf, зрение
    включается там, где текста нет. Для картинки текстового слоя нет по определению.
    """
    return (file_info.get("mimeType") or "").startswith("image/")


def is_parsable(file_info: Dict[str, Any]) -> bool:
    """
    Стоит ли этот файл вообще открывать. Рендеры, фото и видео уходят в зеркало
    (Э6) и в поля-ссылки, но не в модель; сканы документов - исключение, см.
    is_document_scan().
    """
    if is_skipped_document(file_info.get("name"), file_info.get("path", "")):
        return False
    mime = file_info.get("mimeType") or ""
    if mime.startswith(_MEDIA_MIME_PREFIXES):
        return is_document_scan(file_info)
    return True


def route_files_for_gaps(
    files: Iterable[Dict[str, Any]],
    empty_fields: Iterable[str],
    budget: int = DEFAULT_OPEN_BUDGET,
    include_unknown: bool = True,
) -> Dict[str, Any]:
    """
    Главная функция этапа: какие файлы открывать под конкретные пустые поля.

    Возвращает:
      to_open  - список {file, doc_type, fields} в порядке приоритета, не длиннее budget
      skipped  - файлы, отсеянные до всякого открытия, с причиной
      unknown  - опознанные "никак", кандидаты на дорогой fallback (план: открыть
                 в пределах бюджета и классифицировать), уже внутри to_open, если
                 бюджет позволил и include_unknown=True

    Приоритет: сначала файлы, чей тип закрывает хотя бы одно РЕАЛЬНО пустое поле;
    неопознанные - после них, только на остатке бюджета. Файл, закрывающий уже
    заполненные поля, не открывается вовсе - это и есть экономия этапа.
    """
    empty = set(empty_fields or [])
    matched: List[Dict[str, Any]] = []
    unknown: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    for f in files or []:
        name = f.get("name")
        if not is_parsable(f):
            reason = ("identity document" if is_skipped_document(name, f.get("path", ""))
                      else "media, not a source of fields")
            skipped.append({"file": f, "reason": reason})
            continue

        doc_type = classify_document(name, f.get("path", ""))
        if doc_type is None:
            unknown.append({
                "file": f, "doc_type": None, "fields": set(),
                "needs_vision": needs_vision(f),
            })
            continue

        useful = fields_closed_by(doc_type) & empty
        if not useful:
            skipped.append({
                "file": f,
                "reason": f"{doc_type}: closes only already-filled fields",
            })
            continue

        matched.append({
            "file": f, "doc_type": doc_type, "fields": useful,
            "needs_vision": needs_vision(f),
        })

    # Отбор по покрытию РАЗНЫХ полей, а не просто по числу совпадений.
    # Живой прогон на Legal-папке Four Palms показал, почему это принципиально:
    # там 4 zoning-документа и 1 permits, и простая сортировка отдавала все
    # места бюджета zoning'у - четыре дорогих открытия ради ОДНОГО поля
    # Land Zoning Color, а единственный документ PBG, закрывавший отдельное
    # пустое поле, вытеснялся из бюджета вовсе.
    to_open: List[Dict[str, Any]] = []
    covered: Set[str] = set()
    remaining = sorted(matched, key=lambda item: len(item["fields"]), reverse=True)

    while remaining and len(to_open) < budget:
        best = max(remaining, key=lambda item: len(item["fields"] - covered))
        if not (best["fields"] - covered):
            break  # новых полей никто уже не закрывает - дальше только дубли
        to_open.append(best)
        covered |= best["fields"]
        remaining.remove(best)

    # Дубли под уже покрытые поля берём только на остатке бюджета: второй
    # документ по тому же полю - это подстраховка, а не приоритет.
    for item in remaining:
        if len(to_open) >= budget:
            break
        to_open.append(item)
    if include_unknown and len(to_open) < budget and empty:
        to_open.extend(unknown[: budget - len(to_open)])

    logger.info(
        f"📑 Роутинг: {len(to_open)} к открытию (бюджет {budget}), "
        f"{len(matched)} опознано под пустые поля, {len(unknown)} неопознано, "
        f"{len(skipped)} отсеяно без открытия"
    )
    return {"to_open": to_open, "skipped": skipped, "unknown": unknown}
