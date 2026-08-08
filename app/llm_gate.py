"""
Единый выключатель обращений к LLM (Gemini).

Зачем отдельный модуль, а не просто отсутствие GEMINI_API_KEY: без ключа код
вёл себя как будто источник пустой. `parse_message` возвращала
{"is_relevant": False, "reason": "No GEMINI_API_KEY set"} — то есть сообщение
застройщика молча признавалось нерелевантным, а извлечение полей записывало в
Gaps «Gemini API client not initialized» вместо честного «поле не проверялось».
Разница принципиальная: в первом случае карточка выглядит разобранной и
пустой, во втором видно, что разбор не выполнялся.

Пока таблица заполняется вручную (владелец, 06.08.2026), автоматический разбор
намеренно выключен: правила заполнения ещё уточняются, и результат модели по
неготовому контракту пришлось бы потом вычищать. Значения LLM_BACKEND:

    off     - обращений к LLM нет. Извлечение возвращает статус "manual_required",
              вызывающий код обязан отнести поле к пропускам, а не к пустым.
    gemini  - штатный режим, нужен GEMINI_API_KEY.

Ручной разбор ложится в те же upsert_* функции airtable_client, что и
автоматический, поэтому каноны базы (списки select, порядок координат, формат
ключей, пересчёт Gaps) соблюдаются одинаково в обоих режимах.
"""
import logging
import os

logger = logging.getLogger("LLMGate")

BACKEND_OFF = "off"
BACKEND_GEMINI = "gemini"

# Статус в результате извлечения, когда обращение к LLM запрещено. Проверяется
# в вызывающем коде: это НЕ «поле пустое», это «поле не проверялось машиной».
MANUAL_REQUIRED = "manual_required"

_warned = False


def backend() -> str:
    """Текущий режим. По умолчанию off: молчаливый вызов API должен быть
    невозможен без явного включения в .env, а не зависеть от того, оказался
    ли ключ в окружении."""
    value = (os.environ.get("LLM_BACKEND") or BACKEND_OFF).strip().lower()
    if value not in (BACKEND_OFF, BACKEND_GEMINI):
        logger.warning("LLM_BACKEND=%r не распознан, считаю %s", value, BACKEND_OFF)
        return BACKEND_OFF
    return value


def llm_enabled() -> bool:
    """True, только если режим gemini И ключ на месте."""
    if backend() != BACKEND_GEMINI:
        return False
    if not os.environ.get("GEMINI_API_KEY"):
        logger.error("LLM_BACKEND=gemini, но GEMINI_API_KEY не задан — обращений не будет")
        return False
    return True


def note_manual_mode(what: str) -> None:
    """Однократное сообщение в лог, чтобы прогон нельзя было принять за
    отработавший автоматический разбор."""
    global _warned
    if not _warned:
        logger.warning(
            "Автоматический разбор выключен (LLM_BACKEND=%s). "
            "Поля заполняются вручную через tools_manual_intake.py.", backend()
        )
        _warned = True
    logger.info("Пропущено обращение к LLM: %s", what)


def manual_required(what: str) -> dict:
    """Результат-заглушка для мест, где раньше стоял вызов модели."""
    note_manual_mode(what)
    return {"status": MANUAL_REQUIRED, "reason": f"LLM disabled (LLM_BACKEND={backend()}): {what}"}
