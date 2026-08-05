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
import re
import tempfile
from typing import Any, Dict, List, Optional, Set

from app.doc_classification_registry import get_cached_classification, save_classification
from app.doc_router import DEFAULT_OPEN_BUDGET, fields_closed_by, route_files_for_gaps
from app.field_extractor import DOC_MODEL, classify_document_content, extract_field, question_for
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
    exclude_fields: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """
    Главная точка связки: что удалось предложить по пустым полям карточки.

    Возвращает:
      proposals - [{field, value, citation, quotes, needs_human, source_file}]
      gaps      - человекочитаемые записи о том, что НЕ удалось и почему
      opened    - сколько документов реально открыто (для контроля бюджета)

    exclude_fields (владелец, 02.08.2026): шахматка сканируется ПЕРВОЙ и уже
    что-то предложила по этим полям - документы Drive под них не открываем.
    Иначе бюджет открытия документов (по умолчанию 5, папки реально бывают на
    сотни файлов) тратится на то, что уже найдено, а не на то, чего не хватает.

    Ничего не пишет в Airtable: предложения проходят через Confirmed.
    """
    empty = empty_required_fields(project_fields) - (exclude_fields or set())
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
        file_id = f.get("id")
        doc_type = item["doc_type"]
        # None, пока неопознанный файл не классифицирован (кэшем или моделью) -
        # отличает "уже проверили, классифицировать некуда" от "ещё не пытались".
        classified_fallback = False

        if doc_type is not None:
            # Поля, ради которых открываем: только те, что ещё не закрыты
            # предыдущим документом. Иначе второй файл того же типа тратит
            # вызовы модели впустую.
            target_fields = {fl for fl in item["fields"] if fl in still_empty}
            if not target_fields:
                continue
        else:
            # Неопознанный по имени файл (план, Э2, fallback): сначала реестр
            # (app/doc_classification_registry.py) - тот же файл мог уже
            # классифицироваться дорогим вызовом модели в прошлом прогоне по
            # другому проекту, план требует переиспользовать этот результат,
            # а не платить за него снова.
            cached_type = await get_cached_classification(file_id) if file_id else None
            if cached_type:
                target_fields = {fl for fl in fields_closed_by(cached_type) if fl in still_empty}
                if not target_fields:
                    # Классификация из реестра однозначна, и она не про пустые
                    # поля этого прогона - открывать файл незачем вовсе.
                    continue
                doc_type = cached_type
            elif not still_empty:
                # Нечего закрывать вообще - незачем классифицировать файл,
                # даже дёшево из реестра, тем более дорого через модель.
                continue
            else:
                # Реестр не знает файл - классификация решится только после
                # скачивания и извлечения текста (ниже), пока считаем, что
                # закрыть можно любое ещё пустое поле.
                target_fields = {fl for fl in still_empty if question_for(fl)}
                classified_fallback = True

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

            if classified_fallback:
                # Текст есть - классифицируем по-настоящему и записываем в
                # реестр, чтобы следующий прогон (этот или любой другой проект)
                # больше не платил за этот же файл.
                model_type = await classify_document_content(text)
                if model_type:
                    if file_id:
                        await save_classification(
                            file_id, model_type,
                            classified_by="model_fallback", model_used=DOC_MODEL,
                        )
                    doc_type = model_type
                    narrowed = {fl for fl in fields_closed_by(model_type) if fl in still_empty}
                    if not narrowed:
                        # Определили тип, но он не про наши пустые поля -
                        # не тратим вызовы модели на заведомо не те вопросы.
                        gaps.append(f"{name}: classified as '{model_type}', closes no remaining empty field")
                        continue
                    target_fields = narrowed
                # Иначе модель тоже не смогла классифицировать (genuinely
                # unknown) - остаёмся при широком target_fields как последнем
                # средстве, план прямо разрешает открыть непонятный документ
                # "на всякий случай" в пределах бюджета.

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
    exclude_fields: Optional[Set[str]] = None,
) -> Optional[Dict[str, Any]]:
    """
    То же самое, но карточка берётся из живой базы по имени проекта.

    Нужна отдельная функция, потому что вызывающий код (link_fetcher) знает имя
    проекта, но не его текущие поля - а без них нельзя понять, что пусто, и
    роутер начнёт открывать документы под уже заполненные поля.

    exclude_fields пробрасывается в fill_fields_from_drive_files - см. её
    докстринг: это находки шахматки, обработанной раньше документов Drive.

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
        record.get("fields", {}), drive_files, budget=budget, exclude_fields=exclude_fields
    )
    summary["project_id"] = record["id"]
    summary["project_name"] = record.get("fields", {}).get("Project Name")

    if write_gaps:
        await save_findings_to_gaps(record["id"], summary)
    return summary


_CSV_EMPTY_CELLS_RE = re.compile(r"\s*,\s*(?:,\s*)+")


def _readable_csv(text: str) -> str:
    """
    Сворачивает подряд идущие запятые пустых ячеек CSV в ' | ' для читаемости.

    Шахматка попадает в extract_field() как есть, и её дословные цитаты
    выглядели так: «Sanur,,Available units», «Land color,,,,Red,,Trades &
    services city area» - технически верно (значение то же), но нечитаемо
    человеку, который подтверждает предложение (замер 03.08.2026, Mångata).
    Одиночная запятая (обычный текст) не трогается - регулярка требует
    минимум двух подряд.
    """
    return _CSV_EMPTY_CELLS_RE.sub(" | ", text or "")


async def fill_project_fields_from_text(
    project_fields: Optional[Dict[str, Any]],
    text: str,
    source_name: str,
) -> Dict[str, Any]:
    """
    Извлекает пустые поля карточки из ОДНОГО текстового источника целиком -
    без роутинга и бюджета, потому что источник один и его не выбирают.

    Для шахматки (владелец, 02.08.2026): «шахматка сканируется первой, из неё
    заполняется та часть полей, которую можно; дальше в других материалах
    ищем то, чего нет, и не смотрим туда, где не найдём то, что нужно».
    Возвращаемые proposals/gaps идут в тот же формат, что и у
    fill_fields_from_drive_files, чтобы combine_findings мог их объединить
    с находками документов Drive без специального случая.
    """
    text = _readable_csv(text)
    empty = empty_required_fields(project_fields)
    if not empty or not (text or "").strip():
        return {"proposals": [], "gaps": [], "opened": 0}

    proposals: List[Dict[str, Any]] = []
    gaps: List[str] = []
    for field in sorted(empty):
        if not question_for(field):
            continue
        result = await extract_field(text, field)
        result["source_file"] = source_name
        if result.get("accepted"):
            proposals.append(result)
        else:
            gaps.append(f"{field} ({source_name}): {result.get('reason')}")

    logger.info(
        f"📊 Шахматка '{source_name}': {len(proposals)} предложений из {len(empty)} пустых полей"
    )
    return {"proposals": proposals, "gaps": gaps, "opened": 1}


async def run_for_project_from_sheet(
    project_name: str,
    sheet_text: str,
    source_name: str,
    write_gaps: bool = True,
) -> Optional[Dict[str, Any]]:
    """
    То же самое, что run_for_project(), но источник - текст шахматки целиком,
    а не файлы папки Drive. Точка входа для ссылок на Google Sheets: шахматка
    обрабатывается ПЕРВОЙ, и её принятые предложения затем передаются в
    fill_fields_from_drive_files как exclude_fields - документы Drive
    открываются только под то, что шахматка не закрыла.
    """
    import app.airtable_client as ac

    try:
        record = await asyncio.to_thread(_find_project_record, ac, project_name)
    except Exception as e:
        logger.error(f"Не удалось прочитать карточку проекта '{project_name}': {e}")
        return None

    if not record:
        logger.info(f"Проект '{project_name}' не найден в базе - разбор шахматки пропущен")
        return None

    summary = await fill_project_fields_from_text(record.get("fields", {}), sheet_text, source_name)
    summary["project_id"] = record["id"]
    summary["project_name"] = record.get("fields", {}).get("Project Name")

    if write_gaps and (summary["proposals"] or summary["gaps"]):
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


def unwrap_findings(summary: Dict[str, Any]) -> Dict[str, Any]:
    """
    Приводит результат к плоскому виду {proposals, gaps, opened}.

    Разбор документов приходит в двух формах:
      - плоско, как его вернул doc_pipeline.run_for_project();
      - вложенным в 'doc_findings', как его отдаёт link_fetcher.process_generic_link().

    Писатель обязан понимать обе. Пока он понимал только первую, пакетный прогон
    отдавал ему внешний словарь, и proposals/opened молча превращались в [] и 0:
    46 карточек получили секцию AUTO без единого предложения при «открыто
    документов: 0», хотя извлечение отрабатывало (02.08.2026).
    """
    docs = summary.get("doc_findings")
    if not isinstance(docs, dict):
        return summary

    flat = dict(summary)
    flat["proposals"] = docs.get("proposals") or summary.get("proposals") or []
    flat["opened"] = docs.get("opened") or summary.get("opened") or 0
    # gaps верхнего уровня уже включают docs['gaps'] (link_fetcher домерживает их
    # туда), поэтому внешний список приоритетнее - он полнее.
    flat["gaps"] = summary.get("gaps") or docs.get("gaps") or []
    return flat


PROJECT_LINK_FIELDS = [
    "Link to Developer’s Kit (Rus)",
    "Link to Developer’s Kit (Eng)",
    "Availability Chart",
]


def collect_project_links(fields: Dict[str, Any]) -> List[tuple]:
    """
    Ссылки карточки к обработке: шахматки первыми, без повторов одного URL.

    Порядок (владелец, 02.08.2026): шахматка сканируется ПЕРВОЙ, независимо от
    того, в каком поле Airtable она лежит - это таблица, а не свободный текст,
    и написанное в ней прямо (район, зонирование, срок сдачи) надёжнее, чем
    поиск по презентациям вслепую. Закрытые ею поля затем исключаются из
    поиска по остальным материалам.

    Дедупликация по URL обязательна: у Mangata одна и та же таблица указана и
    в 'Link to Developer's Kit (Rus)', и в 'Availability Chart', из-за чего
    извлечение полей отрабатывало по ней дважды и клало в Gaps два одинаковых
    предложения Land Zoning Color (02.08.2026).
    """
    seen = set()
    sheets, others = [], []
    for key in PROJECT_LINK_FIELDS:
        value = fields.get(key)
        if not value:
            continue
        url = str(value).strip()
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        from app.link_fetcher import extract_gsheet_id
        (sheets if extract_gsheet_id(url) else others).append((key, url))
    return sheets + others


def combine_findings(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Объединяет результаты НЕСКОЛЬКИХ ссылок одного проекта в одну сводку перед
    единственной записью в Gaps.

    save_findings_to_gaps ЗАМЕНЯЕТ секцию бота целиком при каждом вызове - так и
    задумано, иначе повторные прогоны плодили бы копии (см. merge_into_gaps).
    Но проект обычно закрыт несколькими ссылками (DevKit Rus + Availability
    Chart), и запись по разу на ссылку означает, что в Gaps остаётся только
    последняя - находки по остальным молча стираются. Обнаружено 02.08.2026 на
    Mangata: разбор папки Drive открыл 5 документов, но результат исчез, как
    только следом обработалась ссылка на таблицу (без doc_findings вообще).

    Вызывающий код должен накопить результаты process_generic_link по всем
    ссылкам проекта и позвать save_findings_to_gaps ОДИН раз с этой сводкой.
    """
    from app.drive_mirror import extract_mirror_airtable_fields

    proposals: List[Dict[str, Any]] = []
    gaps: List[str] = []
    seen_gaps = set()
    opened = 0
    mirror_fields: Dict[str, Any] = {}

    for r in results:
        flat = unwrap_findings(r or {})
        proposals.extend(flat.get("proposals") or [])
        opened += flat.get("opened") or 0
        for g in flat.get("gaps") or []:
            if g not in seen_gaps:
                seen_gaps.add(g)
                gaps.append(g)

        mf = flat.get("mirror_airtable_fields") or extract_mirror_airtable_fields(
            flat.get("drive_mirror") or {}
        )
        for key, value in mf.items():
            mirror_fields.setdefault(key, value)  # первый источник побеждает

    return {
        "proposals": proposals,
        "gaps": gaps,
        "opened": opened,
        "mirror_airtable_fields": mirror_fields,
    }


def format_findings_block(summary: Dict[str, Any]) -> str:
    """
    Текст секции бота. Предложения помечены именно как предложения: значения
    попадают в поля только после Confirmed, и человек, читающий Gaps, должен
    видеть разницу между «нашли и записали» и «нашли, подтвердите».
    """
    summary = unwrap_findings(summary)
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
    Записывает секцию бота в поле Gaps карточки проекта, а также результат
    зеркалирования Drive: ссылку на зеркало в Renders и обложку в Img.

    Извлечённые из документов ЗНАЧЕНИЯ полей карточки по-прежнему не трогаются -
    они доезжают туда только после Confirmed. Renders/Img - это не найденный
    факт, а адрес нашей же копии файлов; оба пишутся только в пустое поле,
    чужую ссылку не затираем.
    """
    import app.airtable_client as ac
    from app.drive_mirror import extract_mirror_airtable_fields

    summary = unwrap_findings(summary)
    mirror_summary = summary.get("drive_mirror") or {}
    mirror_fields = summary.get("mirror_airtable_fields") or extract_mirror_airtable_fields(mirror_summary)
    summary = {**summary, "mirror_airtable_fields": mirror_fields}

    def _write():
        table = ac.get_table("Projects")
        if not table:
            logger.error("Airtable Projects table is unavailable; Gaps were not updated.")
            return False
        record = table.get(project_id)
        fields_dict = record.get("fields", {}) or {}
        current_gaps = fields_dict.get("Gaps")
        merged_gaps = merge_into_gaps(current_gaps, format_findings_block(summary))

        update_payload = {}
        if merged_gaps != (current_gaps or "").strip():
            update_payload["Gaps"] = merged_gaps

        if mirror_fields.get("Renders"):
            current_renders = fields_dict.get("Renders")
            if not current_renders:
                update_payload["Renders"] = mirror_fields["Renders"]
            elif mirror_fields["Renders"] not in str(current_renders):
                # Там уже чужая ссылка (обычно папка застройщика). Менять её -
                # решение владельца, поэтому только сообщаем, а не затираем.
                logger.warning(
                    f"Renders у {project_id} занят другой ссылкой, зеркало не "
                    f"проставлено: {mirror_fields['Renders']}"
                )

        if mirror_fields.get("Img") and not fields_dict.get("Img"):
            update_payload["Img"] = mirror_fields["Img"]

        if not update_payload:
            return False

        result = ac.robust_airtable_op(table.update, project_id, fields=update_payload)
        if not isinstance(result, dict) or not result.get('id'):
            details = result.get('error_details', 'unknown') if isinstance(result, dict) else result
            logger.error(f"❌ Не удалось обновить карточку {project_id}: {details}")
            return False
        return True

    try:
        changed = await asyncio.to_thread(_write)
        if changed:
            logger.info(f"📝 Поля и Gaps обновлены для карточки {project_id}")
        return changed
    except Exception as e:
        logger.error(f"Не удалось записать Gaps для {project_id}: {e}")
        return False
