"""
Связка Э1→Э2: от списка файлов в папке застройщика до предложенных значений полей
(implementation_plan.md).

Порядок ровно по плану, от дешёвого к дорогому:
  1. какие обязательные поля карточки пусты          (app/gaps.py)
  2. какие файлы стоит открыть под эти поля          (app/doc_router.py)
  3. скачать выбранные, извлечь текстовый слой       (app/drive_folder.py, pypdf)
  4. спросить модель по одному полю под валидацией   (app/field_extractor.py)

Модуль НЕ пишет в боевые таблицы. Он возвращает предложения, которые попадают в
карточку только после Confirmed - правило проекта, не деталь реализации: данные
доезжают до Projects/Units лишь после подтверждения человеком (Э4).
"""
import asyncio
import logging
import os
import tempfile
from typing import Any, Dict, List, Optional, Set

from app.doc_router import DEFAULT_OPEN_BUDGET, route_files_for_gaps
from app.field_extractor import extract_field, question_for
from app.gaps import REQUIRED_PROJECT_FIELDS, is_filled

logger = logging.getLogger("DocPipeline")


def empty_required_fields(project_fields: Optional[Dict[str, Any]]) -> Set[str]:
    """
    Ключи (не человекочитаемые ярлыки) обязательных полей, которые сейчас пусты.

    app/gaps.py отдаёт ярлыки для сообщения застройщику («срок аренды (лет)»),
    а роутеру нужны имена полей Airtable - на них построена карта DOC_TYPE_TO_FIELDS.
    """
    fields = project_fields or {}
    return {key for key in REQUIRED_PROJECT_FIELDS if not is_filled(fields.get(key))}


def _suffix_for(file_info: Dict[str, Any]) -> str:
    name = file_info.get("name") or ""
    _, ext = os.path.splitext(name)
    return ext if ext else ".bin"


async def _extract_text(path: str) -> str:
    from app.doc_parser import extract_text_from_pdf

    if not path.lower().endswith(".pdf"):
        return ""
    return await asyncio.to_thread(extract_text_from_pdf, path)


async def fill_fields_from_drive_files(
    project_fields: Optional[Dict[str, Any]],
    drive_files: List[Dict[str, Any]],
    budget: int = DEFAULT_OPEN_BUDGET,
) -> Dict[str, Any]:
    """
    Главная точка связки: что удалось предложить по пустым полям карточки.

    Возвращает:
      proposals - [{field, value, citation, quotes, needs_human, source_file}]
      gaps      - человекочитаемые записи о том, что НЕ удалось и почему
      opened    - сколько документов реально открыто (для контроля бюджета)

    Ничего не пишет в Airtable: предложения проходят через Confirmed.
    """
    empty = empty_required_fields(project_fields)
    if not empty:
        return {"proposals": [], "gaps": [], "opened": 0}

    routing = route_files_for_gaps(drive_files, empty, budget=budget)
    proposals: List[Dict[str, Any]] = []
    gaps: List[str] = []
    opened = 0
    still_empty = set(empty)

    for item in routing["to_open"]:
        f = item["file"]
        name = f.get("name") or f.get("id")

        # Поля, ради которых открываем: только те, что ещё не закрыты предыдущим
        # документом. Иначе второй файл того же типа тратит вызовы модели впустую.
        target_fields = {fl for fl in item["fields"] if fl in still_empty}
        if item["doc_type"] is None:
            # Неопознанный документ (fallback по решению владельца): под какое
            # поле его спрашивать, неизвестно - пробуем все ещё пустые, для
            # которых вообще заведён узкий вопрос.
            target_fields = {fl for fl in still_empty if question_for(fl)}
        if not target_fields:
            continue

        if item.get("needs_vision"):
            # Скан без текстового слоя. Сверять цитату не с чем, а значение без
            # проверки в базу не пишется (см. app/citations.is_verifiable_source).
            # Предохранитель для сканов - прогон двумя моделями со сверкой ответов -
            # в плане отмечен как непроверенный кандидат и здесь не реализован.
            gaps.append(f"{name}: scan without text layer, needs vision + dual-model check (not implemented)")
            continue

        tmp_path = None
        try:
            from app.drive_folder import download_drive_file

            fd, tmp_path = tempfile.mkstemp(suffix=_suffix_for(f))
            os.close(fd)
            ok = await asyncio.to_thread(download_drive_file, f["id"], tmp_path)
            if not ok:
                gaps.append(f"{name}: download failed")
                continue

            opened += 1
            text = await _extract_text(tmp_path)
            if not text.strip():
                gaps.append(f"{name}: no text layer extracted")
                continue

            for field in sorted(target_fields):
                result = await extract_field(text, field)
                result["source_file"] = name
                if result.get("accepted"):
                    proposals.append(result)
                    still_empty.discard(field)
                else:
                    gaps.append(f"{field} ({name}): {result.get('reason')}")
        except RuntimeError as e:
            # OAuth ещё не настроен - ожидаемое состояние, не повод падать.
            gaps.append(f"{name}: Drive access unavailable ({e})")
        except Exception as e:
            logger.error(f"Ошибка разбора {name}: {e}")
            gaps.append(f"{name}: parsing failed ({e})")
        finally:
            # Временные файлы не задерживаются на диске (правило проекта).
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)

    logger.info(
        f"📄 Разбор документов: {len(proposals)} предложений, {opened} открыто, "
        f"{len(gaps)} пробелов, поля без ответа: {sorted(still_empty)}"
    )
    return {"proposals": proposals, "gaps": gaps, "opened": opened}


async def run_for_project(
    project_name: str,
    drive_files: List[Dict[str, Any]],
    budget: int = DEFAULT_OPEN_BUDGET,
    write_gaps: bool = True,
) -> Optional[Dict[str, Any]]:
    """
    То же самое, но карточка берётся из живой базы по имени проекта.

    Нужна отдельная функция, потому что вызывающий код (link_fetcher) знает имя
    проекта, но не его текущие поля - а без них нельзя понять, что пусто, и
    роутер начнёт открывать документы под уже заполненные поля.

    Возвращает None, если проект в базе не найден: открывать документы «на
    всякий случай», не зная чего не хватает, план запрещает прямо.
    """
    import app.airtable_client as ac

    try:
        record = await asyncio.to_thread(_find_project_record, ac, project_name)
    except Exception as e:
        logger.error(f"Не удалось прочитать карточку проекта '{project_name}': {e}")
        return None

    if not record:
        logger.info(f"Проект '{project_name}' не найден в базе - разбор документов пропущен")
        return None

    summary = await fill_fields_from_drive_files(
        record.get("fields", {}), drive_files, budget=budget
    )
    summary["project_id"] = record["id"]
    summary["project_name"] = record.get("fields", {}).get("Project Name")

    if write_gaps:
        await save_findings_to_gaps(record["id"], summary)
    return summary


def _find_project_record(ac, project_name: str) -> Optional[Dict[str, Any]]:
    """Поиск карточки тем же нечётким сопоставлением, что и весь остальной код."""
    ac.init_cache()
    return ac.find_project_by_query(project_name, ac.CACHE_PROJECTS)


# Границы секции бота внутри поля Gaps. В Gaps уже лежат рукописные разборы
# (у Four Palms - подробный отчёт с номерами регистраций и именами), и затирать
# их нельзя ни при каких условиях. Бот владеет только текстом между маркерами.
GAPS_SECTION_START = "--- AUTO: разбор документов (бот) ---"
GAPS_SECTION_END = "--- /AUTO ---"


def format_findings_block(summary: Dict[str, Any]) -> str:
    """
    Текст секции бота. Предложения помечены именно как предложения: значения
    попадают в поля только после Confirmed, и человек, читающий Gaps, должен
    видеть разницу между «нашли и записали» и «нашли, подтвердите».
    """
    lines = [GAPS_SECTION_START]

    proposals = summary.get("proposals") or []
    if proposals:
        lines.append("ПРЕДЛОЖЕНО (требует подтверждения, в поля не записано):")
        for p in proposals:
            mark = " [юр. поле, только с Confirmed]" if p.get("needs_human") else ""
            lines.append(f"  {p['field']} = {p['value']}{mark}")
            lines.append(f"      источник: {p.get('source_file')}, цитата: {p.get('citation')}")
            for q in (p.get("quotes") or [])[:2]:
                lines.append(f"      «{str(q)[:120]}»")

    gaps = summary.get("gaps") or []
    if gaps:
        lines.append("НЕ ЗАКРЫТО документами:")
        for g in gaps:
            lines.append(f"  - {g}")

    if not proposals and not gaps:
        lines.append("  разбор не дал ни предложений, ни новых пробелов")

    lines.append(f"(открыто документов: {summary.get('opened', 0)})")
    lines.append(GAPS_SECTION_END)
    return "\n".join(lines)


def merge_into_gaps(existing: Optional[str], block: str) -> str:
    """
    Вставляет секцию бота в существующий текст Gaps, заменяя ТОЛЬКО свою
    предыдущую секцию. Рукописный текст вокруг остаётся нетронутым, повторный
    прогон не плодит копии.
    """
    existing = existing or ""
    start = existing.find(GAPS_SECTION_START)
    if start == -1:
        return (existing.rstrip() + "\n\n" + block).strip()

    end = existing.find(GAPS_SECTION_END, start)
    if end == -1:
        # Секция открыта, но не закрыта (обрезали руками) - заменяем до конца.
        return (existing[:start].rstrip() + "\n\n" + block).strip()

    tail = existing[end + len(GAPS_SECTION_END):]
    return (existing[:start].rstrip() + "\n\n" + block + tail.rstrip()).strip()


async def save_findings_to_gaps(project_id: str, summary: Dict[str, Any]) -> bool:
    """
    Записывает секцию бота в поле Gaps карточки проекта.

    Пишется ТОЛЬКО Gaps: сами поля карточки не трогаются, значения доезжают до
    них после Confirmed. Существующий текст Gaps сохраняется целиком.
    """
    import app.airtable_client as ac

    def _write():
        table = ac.get_base().table("Projects")
        record = table.get(project_id)
        current = (record.get("fields", {}) or {}).get("Gaps")
        merged = merge_into_gaps(current, format_findings_block(summary))
        if merged == (current or "").strip():
            return False
        table.update(project_id, {"Gaps": merged})
        return True

    try:
        changed = await asyncio.to_thread(_write)
        if changed:
            logger.info(f"📝 Gaps обновлены для карточки {project_id}")
        return changed
    except Exception as e:
        logger.error(f"Не удалось записать Gaps для {project_id}: {e}")
        return False
