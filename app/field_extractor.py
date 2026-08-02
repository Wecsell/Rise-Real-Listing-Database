"""
Извлечение ОДНОГО поля карточки из документа под валидацией цитат
(implementation_plan.md, Э2 + «Правила, следующие из этого»).

Отличие от app/doc_parser.py принципиальное, а не стилистическое.
`parse_pdf_document()` задаёт модели открытый вопрос («извлеки все данные по
проекту и юнитам») - для брошюры это нормально, для юридического документа
запрещено планом: именно на открытых вопросах слабая модель уверенно выдаёт
срок аренды 17 лет из штрафной статьи вместо 35 из статьи 1.

Здесь каждый вызов - одно поле, с перечисленными допустимыми значениями,
жёсткой схемой ответа и обязательной дословной цитатой, которая программно
сверяется с исходным текстом. Не прошла сверку - значение не пишется никуда,
уходит в Gaps и в вопрос агенту. Задача модели - извлечение, а не суждение.
"""
import json
import logging
import re
from typing import Any, Dict, List, Optional

from google.genai import types

from app.citations import check_quotes, is_verifiable_source, normalize, BAD, OK, SPLICED
from app.gemini_parser import client, resolve_model_name

logger = logging.getLogger("FieldExtractor")

# Уровень T2 из плана: документы, низкий объём вызовов. Единственная модель со
# 100% на обоих наборах замера и чистыми цитатами. Дороже за токен, но объём мал.
DOC_MODEL = "gemini-3.5-flash"

# Юридические и рисковые поля модель не заполняет сама (план, п.6): её ответ -
# только предложение, которое подтверждает человек через Confirmed.
HUMAN_CONFIRM_FIELDS = {
    "Handover Permits",
    "Land Zoning Color",
    "Lease Term (years)",
    "Renewal Right",
    "Ownership Type",
}

SYSTEM_PROMPT = """You extract ONE fact from a real-estate document.

Rules:
- Answer ONLY from the document text provided. Never use outside knowledge, never infer.
- You MUST provide `quotes`: a list of verbatim substrings copied EXACTLY from the
  document, character for character, that support your answer. Do not paraphrase and do
  not stitch separate fragments into one string - put each contiguous fragment as its own
  item in the list. If your answer rests on two documents or two rows, give one quote per source.
- If the document does not state the answer, set answer to "not stated" and quotes to [].
- Beware of numbers that appear in a different context than the one asked about.

Return strictly this JSON:
{"answer": "<short answer>", "quotes": ["<verbatim fragment>", "..."], "confidence": <0.0-1.0>}
"""

# Что именно спрашивать под каждое пустое поле. Открытых формулировок здесь быть
# не должно: вопрос узкий, поле одно - при таком условии замер (90 прогонов,
# 5 моделей) не дал ни одного попадания в ловушку.
FIELD_QUESTIONS: Dict[str, str] = {
    "Lease Term (years)": (
        "For how many years is the land leased to the buyer, counting from project "
        "completion, as the developer presents it? Answer with a number of years only."
    ),
    "Ownership Type": "Is the ownership leasehold or freehold?",
    "Land Zoning Color": "What is the land zoning designation of this plot?",
    "Handover Permits": (
        "Has a building permit (PBG, SLF or IMB) actually been ISSUED for this project? "
        "A zoning document (ITR/PKKPR) is NOT a building permit. An application that is "
        "still in process is NOT an issued permit."
    ),
    "Developer": "What is the full legal name of the company that owns or develops this project?",
    "Price From (USD)": "What is the lowest price of a unit, and in what currency?",
}

_NOT_STATED = ("not stated", "not stated.", "unknown", "n/a", "none", "")

# Поля, которые заполняются ТОЛЬКО положительной находкой (план, «Соглашения по
# полям» + Э3 «Правило достоверности»). Отсутствие документа в папке не равно
# отсутствию разрешения: «разрешения нет» имеет право сказать только агент,
# через Э4, и никогда - разбор документа.
POSITIVE_ONLY_FIELDS = {"Handover Permits"}

# Отрицание в ответе модели. Границы слов обязательны: подстрока "no" сидит
# внутри "notarial", "nomor", "not" внутри "notice" - тот же класс бага, что
# "are" внутри "share" в предфильтре listener.py и "has been issued" внутри
# "has NOT been issued" в бенчмарке.
_NEGATIVE_ANSWER_RE = re.compile(
    r"\bno\b|\bnot\b|\bnever\b|\bnone\b|\bwithout\b|\bbelum\b|\btidak\b|"
    r"\bin process\b|\bin progress\b|\bpending\b|\bperbaikan\b",
    re.IGNORECASE,
)

# Плейсхолдер из шаблона ответа в промпте: "<short answer>", "<verbatim fragment>".
_PLACEHOLDER_RE = re.compile(r"^\s*<.*>\s*$")


def _parse_model_json(raw: str) -> Dict[str, Any]:
    """
    Разбирает ответ модели, которая может вернуть НЕСКОЛЬКО JSON-объектов подряд.

    Часть моделей сначала повторяет шаблон из промпта, и только потом даёт
    настоящий ответ - json.loads на такой строке падает с "Extra data".
    Бенчмарк на этом месте берёт первый валидный объект, но первым как раз и
    оказывается шаблон с плейсхолдером "<short answer>" и пустыми цитатами,
    то есть измеряется шаблон, а не ответ модели. Здесь плейсхолдеры
    пропускаются: берётся первый объект с осмысленным ответом.

    Безопасность от этого не страдает: что бы ни выбралось, значение всё равно
    обязано пройти сверку цитат с исходным текстом.
    """
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\n?|```$", "", raw).strip()

    decoder = json.JSONDecoder()
    idx = raw.find("{")
    candidates: List[Dict[str, Any]] = []
    while idx >= 0:
        try:
            obj, end = decoder.raw_decode(raw[idx:])
        except ValueError:
            break
        if isinstance(obj, dict):
            candidates.append(obj)
        next_idx = raw.find("{", idx + end)
        if next_idx <= idx:
            break
        idx = next_idx

    if not candidates:
        raise ValueError("модель не вернула ни одного JSON-объекта")

    for obj in candidates:
        answer = str(obj.get("answer", ""))
        if answer and not _PLACEHOLDER_RE.match(answer):
            return obj
    return candidates[0]


def question_for(field: str) -> Optional[str]:
    return FIELD_QUESTIONS.get(field)


_CLASSIFY_SYSTEM_PROMPT = """You classify ONE real-estate document by its type.

Rules:
- Answer using ONLY the text provided, no outside knowledge.
- Pick exactly one label from the allowed list, or "unknown" if none genuinely fits.
- Do not guess: an unclear or unrelated document must be "unknown", not a forced label.

Return strictly this JSON:
{"doc_type": "<one label from the list, or unknown>", "confidence": <0.0-1.0>}
"""

_DOC_TYPE_DESCRIPTIONS = {
    "lease": "land lease / rental agreement (SMT, sewa, akta sewa)",
    "zoning": "land zoning designation document (ITR, PKKPR, KKPR)",
    "permits": "building/occupancy permit (PBG, SLF, IMB)",
    "company": "developer company registration document (akta pendirian, NIB, NPWP)",
    "pricing": "unit pricing or specification sheet",
    "certificate": "land title certificate (SHM, SHGB)",
}


async def classify_document_content(text: str, model: str = DOC_MODEL) -> Optional[str]:
    """
    Классифицирует ДОКУМЕНТ (implementation_plan.md, Э2, fallback для файлов,
    не опознанных по имени) - не поле, поэтому вне валидации цитат из extract_field:
    ошибочная классификация здесь максимум приводит к неверному узкому вопросу,
    а итоговое значение поля всё равно проходит свою собственную проверку цитаты.

    Возвращает один из ключей app.doc_router.DOC_TYPE_TO_FIELDS или None, если
    модель ответила "unknown" либо вернула что-то не из списка - в реестр
    попадает только правдоподобный тип, не мусор.
    """
    if not client or not (text or "").strip():
        return None

    from app.doc_router import DOC_TYPE_TO_FIELDS

    labels = sorted(DOC_TYPE_TO_FIELDS.keys())
    listing = "\n".join(f"- {label}: {_DOC_TYPE_DESCRIPTIONS.get(label, label)}" for label in labels)
    # Нарезка входа (план, п.4): классификации достаточно начала документа,
    # первая страница/заголовок обычно и определяет тип формы.
    excerpt = (text or "")[:4000]

    try:
        resp = await client.aio.models.generate_content(
            model=model or resolve_model_name(),
            contents=f"ALLOWED LABELS:\n{listing}\n\nDOCUMENT:\n{excerpt}",
            config=types.GenerateContentConfig(
                system_instruction=_CLASSIFY_SYSTEM_PROMPT,
                response_mime_type="application/json",
                temperature=0.0,
            ),
        )
        data = _parse_model_json(resp.text)
    except Exception as e:
        logger.error(f"Ошибка классификации документа: {e}")
        return None

    doc_type = str(data.get("doc_type", "")).strip().lower()
    if doc_type not in labels:
        return None
    return doc_type


def select_relevant_pages(text: str, field: str, max_pages: int = 2) -> str:
    """
    Нарезка входа (план, п.4): не отдавать модели 10 страниц. Ищем страницы по
    ключевым словам поля и отдаём 1-2 - меньше вход, меньше дрейф.

    Текст ожидается в формате extract_text_from_pdf(): страницы разделены
    заголовком "--- Страница N ---". Если разметки страниц нет (обычный текст),
    возвращаем как есть.
    """
    from app.doc_router import DOC_TYPE_PATTERNS, DOC_TYPE_TO_FIELDS

    pages = re.split(r"(?=--- Страница \d+ ---)", text or "")
    pages = [p for p in pages if p.strip()]
    if len(pages) <= max_pages:
        return text or ""

    patterns = [pat for doc_type, pat in DOC_TYPE_PATTERNS
                if field in DOC_TYPE_TO_FIELDS.get(doc_type, set())]
    if not patterns:
        return "\n\n".join(pages[:max_pages])

    combined = re.compile("|".join(patterns), re.IGNORECASE)
    scored = [(len(combined.findall(p)), i, p) for i, p in enumerate(pages)]
    scored.sort(key=lambda t: (-t[0], t[1]))

    if scored[0][0] == 0:
        # Ни одна страница не упоминает тему поля - берём начало документа,
        # а не случайные страницы: врать про релевантность хуже, чем не угадать.
        return "\n\n".join(pages[:max_pages])

    chosen = sorted(scored[:max_pages], key=lambda t: t[1])
    return "\n\n".join(p for _, _, p in chosen)


def is_negative_answer(answer: str) -> bool:
    """Модель ответила отрицанием («разрешение не выдано», «заявка в процессе»)."""
    return bool(_NEGATIVE_ANSWER_RE.search(answer or ""))


def _verdict(answer: str, quotes: Any, source: str, field: str = "") -> Dict[str, Any]:
    """
    Решение о судьбе значения. Отдельно от вызова модели, чтобы правила были
    проверяемы тестами без сети.
    """
    if field in POSITIVE_ONLY_FIELDS and is_negative_answer(answer):
        # Живой прогон на ловушках Four Palms (тесты 11 и 12 плана): модель
        # честно отвечает «ITR это не разрешение» и «статус Perbaikan Dokumen,
        # выдано 0». В ловушку она не попалась - но записывать этот ответ в поле
        # всё равно нельзя: Handover Permits принимает только положительную
        # находку, а отрицание уходит агенту в Э3.
        return {"accepted": False, "citation": "n/a",
                "reason": "negative finding: positive-only field, goes to the agent question"}

    if normalize(answer) in _NOT_STATED:
        # Отказ - штатный исход, а не сбой: поле остаётся пустым и уходит в
        # вопрос агенту. Ошибка в безопасную сторону.
        return {"accepted": False, "reason": "not stated in document", "citation": "n/a"}

    if not is_verifiable_source(source):
        # Чистый скан: сверять не с чем, проверка неприменима. Значение нельзя
        # молча считать подтверждённым (план: 3 из 5 ответов на сканах были
        # непроверяемыми, включая самые рискованные документы).
        return {"accepted": False, "reason": "unverifiable source (scan, no text layer)",
                "citation": "n/a"}

    citation = check_quotes(quotes, source)
    if citation == BAD:
        return {"accepted": False, "reason": "citation not found in source", "citation": BAD}
    return {"accepted": True, "reason": None, "citation": citation}


async def extract_field(
    text: str,
    field: str,
    question: Optional[str] = None,
    model: str = DOC_MODEL,
) -> Dict[str, Any]:
    """
    Спрашивает модель об одном поле и пропускает ответ через валидацию.

    Возвращает:
      field, value            - значение или None, если оно не прошло проверку
      accepted                - записывать ли его вообще
      needs_human             - юридическое/рисковое поле: только предложение,
                                подтверждает человек через Confirmed (план, п.6)
      citation                - 'ok' | 'spliced' | 'bad' | 'n/a'
      quotes, confidence, reason
    """
    question = question or question_for(field)
    if not question:
        return {"field": field, "value": None, "accepted": False,
                "reason": f"no narrow question defined for field '{field}'"}

    if not client:
        return {"field": field, "value": None, "accepted": False,
                "reason": "Gemini API client not initialized"}

    source = select_relevant_pages(text, field)

    try:
        resp = await client.aio.models.generate_content(
            model=model or resolve_model_name(),
            contents=f"DOCUMENT:\n{source}\n\nQUESTION: {question}",
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                temperature=0.0,
            ),
        )
        data = _parse_model_json(resp.text)
    except Exception as e:
        logger.error(f"Ошибка извлечения поля {field}: {e}")
        return {"field": field, "value": None, "accepted": False, "reason": str(e)[:200]}

    answer = str(data.get("answer", ""))
    quotes = data.get("quotes", data.get("quote", []))
    verdict = _verdict(answer, quotes, source, field)

    accepted = verdict["accepted"]
    logger.info(
        f"🔍 {field}: {'принято' if accepted else 'отклонено'} "
        f"({verdict['reason'] or verdict['citation']}) -> {answer[:60]}"
    )
    return {
        "field": field,
        "value": answer if accepted else None,
        "accepted": accepted,
        "needs_human": field in HUMAN_CONFIRM_FIELDS,
        "citation": verdict["citation"],
        "quotes": quotes,
        "confidence": data.get("confidence"),
        "reason": verdict["reason"],
    }


def gaps_from_results(results: List[Dict[str, Any]]) -> List[str]:
    """
    Отклонённые значения обязаны быть видны: непрошедшая цитата, конфликт или
    непроверяемый скан - это Gaps и вопрос агенту, а не молчаливый пропуск.
    """
    gaps = []
    for r in results or []:
        if r.get("accepted"):
            continue
        reason = r.get("reason") or "rejected"
        gaps.append(f"{r.get('field')}: {reason}")
    return gaps
