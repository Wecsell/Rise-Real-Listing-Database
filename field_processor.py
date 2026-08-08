import os
import json
import asyncio
import logging
from dotenv import load_dotenv
from google import genai
from google.genai import types

# Runtime-provided environment variables must take precedence over a local
# development .env file.  In particular, a production container must not be
# redirected to a test Airtable base by an accidental local file.
load_dotenv(override=False)
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

# httpx и telethon на уровне INFO пишут полный URL запроса, а токены стоят
# прямо в пути. При выводе в файл они оказываются на диске открытым текстом.
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)

logger = logging.getLogger("FieldProcessor")

from app import llm_gate

# Клиент создаётся только через общий рубильник, как в gemini_parser. Раньше
# здесь стояла проверка на один лишь GEMINI_API_KEY — из-за неё LLM_BACKEND=off
# на этот процесс не действовал, а при пустом ключе каждая находка помечалась
# Status='Error' вместо честного "разбор не выполнялся" (найдено 07.08.2026).
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if llm_gate.llm_enabled() else None

from app.gemini_parser import SYSTEM_PROMPT, resolve_model_name
from app.priority_parser import build_update_fields
from app.gaps import project_gaps, unit_gaps, developer_gaps, merge_gaps
from app.staging import (PARSED_JSON_FIELD, load_parsed, needs_parsing,
                         promotion_blockers, should_promote)
from app.naming import swap_coordinates
from app.dedup import (build_contact_notice, classify_contact_match,
                       describe_contact_match, extract_phones, find_matches,
                       notify_lister)
from app.airtable_client import field_exists, get_table
from app.url_safety import stream_response_to_tempfile, stream_safe_url


def _staging_table():
    """Resolve Field Staging through Airtable metadata, never its display name.

    The active token can access records only by table ID.  Resolving lazily
    keeps import-time tests offline and fails before any read or write if the
    schema cannot be verified.
    """
    table = get_table("Field Staging")
    if table is None:
        raise RuntimeError("Field Staging table ID is unavailable from Airtable metadata")
    return table


def _positive_int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r; using %s", name, os.environ.get(name), default)
        return default


# Attachments in Field Staging are produced by Airtable's CDN.  ``api.telegram.org``
# is retained only so pre-existing staging rows created by earlier versions can
# still be processed.  Any other host is rejected before a connection is made.
FIELD_ATTACHMENT_HOSTS = frozenset({
    "airtable.com",
    "*.airtable.com",
    "airtableusercontent.com",
    "*.airtableusercontent.com",
    "api.telegram.org",
})
FIELD_ATTACHMENT_MAX_BYTES = _positive_int_env(
    "FIELD_ATTACHMENT_MAX_BYTES", 25 * 1024 * 1024
)
FIELD_ATTACHMENT_TIMEOUT_SECONDS = _positive_int_env(
    "FIELD_ATTACHMENT_TIMEOUT_SECONDS", 30
)
FIELD_GEMINI_TIMEOUT_SECONDS = _positive_int_env(
    "FIELD_GEMINI_TIMEOUT_SECONDS", 120
)
FIELD_PARSED_JSON_MAX_CHARS = _positive_int_env(
    "FIELD_PARSED_JSON_MAX_CHARS", 95_000
)

FIELD_PROMPT = SYSTEM_PROMPT + """

ВНИМАНИЕ: К тебе поступили материалы с "поля" (с улицы). Ты получишь ФОТО баннера/виллы и АУДИОЗАПИСЬ (комментарий агента).
Также тебе будут переданы GPS КООРДИНАТЫ.
Твоя задача:
1. Расшифровать аудиозапись, где агент диктует свои мысли (название, цену, кол-во спален, стадию строительства).
2. Извлечь текст с фото (имена проектов, застройщиков, телефоны, сайты).
3. Взять GPS координаты и поместить их в Project -> 'Coordinates(for Map)'.

3. ПРИОРИТЕТ ОБЪЕКТА (Priority):
   - КРИТИЧЕСКОЕ ПРАВИЛО: Приоритет определяется ИСКЛЮЧИТЕЛЬНО по личным ГОЛОСОВЫМ комментариям агента из аудио!
   - МАРКЕТИНГ С БАННЕРОВ: Рекламные слоганы и эпитеты с фото/баннера ("Luxury", "роскошный", "дизайнерский", "Exclusive", "5 звезд", "Best Villa") НЕ ПОДНИМАЮТ приоритет до "Высокий"! Это реклама застройщика, а не отзыв агента.
   - "Высокий" (Hight) — выставляется ТОЛЬКО если агент ЛИЧНО в аудиозаписи хвалит проект ("агент отмечает/считает, что проект топовый, отличный, супер, бомба, перспективный") или прямо говорит "приоритет высокий / срочно".
   - "Низкий" (Low) — выставляется при негативной оценке проекта/застройщика ("ужасная стройка", "мутный застройщик", "не нравится"), а также при УПОМИНАНИИ ЮРИДИЧЕСКИХ РИСКОВ: "зеленая земля / зеленая зона", "нет документов", "проблемы с документами", "нет разрешения / PBG".
   - "Средний" (Medium) — во всех остальных случаях (по умолчанию), включая рекламные слоганы с баннеров и состояние баннеров/заборов.

4. Свести все это в единый JSON, включив верхнеуровневое поле 'Priority': "Высокий" | "Средний" | "Низкий".
"""


def _serialize_parsed_finding(parsed: dict) -> str:
    """Serialize a staging parse without ever storing malformed JSON.

    Airtable's long-text limit is finite.  Truncating serialized JSON halfway
    through a string made a record look ``Processed`` while making it
    impossible to promote later.  A too-large result is now a visible error
    that can be retried or reviewed with its source material intact.
    """
    serialized = json.dumps(parsed, ensure_ascii=False)
    if len(serialized) > FIELD_PARSED_JSON_MAX_CHARS:
        raise ValueError(
            "Parsed JSON exceeds Field Staging capacity "
            f"({len(serialized)} > {FIELD_PARSED_JSON_MAX_CHARS} characters)"
        )
    return serialized

async def download_file(url: str, suffix: str) -> str:
    """Stream one trusted staging attachment into a bounded temporary file.

    Field records are data supplied by users, even if they live in Airtable.
    The common URL guard therefore validates scheme, allow-listed host, DNS,
    every redirect and response size before Gemini is given the file.
    """
    async with stream_safe_url(
        url,
        timeout=float(FIELD_ATTACHMENT_TIMEOUT_SECONDS),
        allowed_hosts=FIELD_ATTACHMENT_HOSTS,
    ) as response:
        if response.status_code != 200:
            raise RuntimeError(
                f"Attachment download returned HTTP {response.status_code}"
            )
        return await stream_response_to_tempfile(
            response,
            suffix=suffix,
            max_bytes=FIELD_ATTACHMENT_MAX_BYTES,
        )

async def process_staging_records():
    """Полный цикл: разбор новых находок, затем перенос подтвержденных."""
    await parse_new_findings()
    await promote_confirmed_findings()


async def promote_confirmed_findings():
    """
    Переносит в Developer/Projects/Units находки с отметкой Confirmed.

    Идемпотентность: признак выполненного переноса — заполненная связь Project.
    Раньше записи создавались в основной базе и только потом помечались
    Processed; падение между этими шагами приводило к повторному созданию на
    следующем тике через 30 секунд. Дубли, с которыми в проекте боролись
    скриптами merge/check_duplicates, рождались именно здесь.
    """
    from app.airtable_client import (init_cache_async, upsert_developer,
                                     upsert_project, upsert_unit)

    records = await asyncio.to_thread(_staging_table().all, formula="{Confirmed} = 1")
    pending = [r for r in records if should_promote(r)]

    for rec in records:
        if rec not in pending:
            logger.info(f"[{rec['id']}] перенос пропущен: {', '.join(promotion_blockers(rec))}")
    if not pending:
        return

    logger.info(f"Подтвержденных записей: {len(records)}, к переносу: {len(pending)}")

    # Свежий кэш перед поиском дублей: иначе проект, созданный другим процессом
    # или добавленный руками, не найдется и продублируется.
    await init_cache_async(force=True)

    for rec in pending:
        rec_id = rec['id']
        parsed = load_parsed(rec)
        fields = rec.get('fields', {})

        try:
            dev_data = parsed.get('Developer', {}) or {}
            proj_data = parsed.get('Projects', {}) or {}
            units = parsed.get('Units', []) or []

            if not dev_data.get('Developer'):
                dev_data['Developer'] = 'Unknown'
            dev_id = await upsert_developer(dev_data)
            if not dev_id:
                raise RuntimeError("Developer upsert failed; Field Staging record remains unlinked")

            if fields.get('Coordinates'):
                # В Field Staging координаты лежат как их прислал Telegram:
                # широта, долгота. Карте Airtable нужен обратный порядок.
                proj_data['Coordinates(for Map)'] = swap_coordinates(fields['Coordinates'])

            proj_gaps = merge_gaps(
                project_gaps(proj_data),
                developer_gaps(dev_data),
                parsed.get('Gaps'),
            )
            logger.info(f"[{rec_id}] незаполненных полей: {len(proj_gaps)} -> {proj_gaps}")

            proj_id = await upsert_project(proj_data, dev_id, gaps=proj_gaps)
            if not proj_id:
                raise RuntimeError("Project upsert failed; Field Staging record remains unlinked")

            for unit_index, u in enumerate(units, start=1):
                unit_id = await upsert_unit(
                    u, proj_id, proj_data.get('Project Name', 'None'), unit_gaps(u)
                )
                if not unit_id:
                    raise RuntimeError(
                        f"Unit {unit_index} upsert failed; Field Staging record remains unlinked"
                    )

            final_proj_id = proj_id[0] if isinstance(proj_id, list) and proj_id else (
                proj_id if isinstance(proj_id, str) else None)
            if not final_proj_id:
                raise RuntimeError("Project upsert returned no record ID")

            # Связи проставляем последним шагом — именно они помечают запись
            # как перенесенную и не дают обработать ее второй раз.
            links = {}
            if dev_id:
                links['Developer'] = [dev_id]
            if final_proj_id:
                links['Project'] = [final_proj_id]
            if links:
                await asyncio.to_thread(_staging_table().update, rec_id, links)

            logger.info(f"[{rec_id}] перенесена в основную базу (проект {final_proj_id})")

        except Exception as e:
            logger.error(f"[{rec_id}] перенос не удался: {e}")


async def parse_new_findings():
    # Разбор выключен рубильником — находки ОСТАЮТСЯ в статусе 'New' и ждут
    # включения. Прежний код доходил до gemini_client=None, падал на upload и
    # ставил Status='Error': находка выглядела бракованной, хотя её просто
    # никто не смотрел, и после включения модели она бы уже не подхватилась
    # фильтром {Status}='New'.
    if gemini_client is None:
        llm_gate.note_manual_mode("parse_new_findings (Field Staging)")
        return

    logger.info("Проверяем новые записи в Field Staging...")
    # Берем все записи со Status = New
    # Формула — первичный фильтр, needs_parsing — страховка на случай, если
    # статус изменился между запросом и обработкой.
    records = [r for r in await asyncio.to_thread(_staging_table().all, formula="{Status} = 'New'")
               if needs_parsing(r)]

    if not records:
        logger.info("Нет новых записей для обработки.")
        return

    logger.info(f"Найдено новых находок: {len(records)}")

    # Для поиска дублей нужны все находки, а не только новые: совпадение
    # ищется с тем, что уже лежит в базе.
    all_findings = await asyncio.to_thread(_staging_table().all, fields=['Contact', 'Submitted By', 'Id'])

    for rec in records:
        rec_id = rec['id']
        fields = rec.get('fields', {})
        
        photos = fields.get('Photo', [])
        audios = fields.get('Audio', [])
        coords = fields.get('Coordinates', '')
        
        logger.info(f"\n--- Обработка записи {rec_id} ---")
        
        uploaded_files = []
        temp_files = []
        
        try:
            if photos:
                for idx, item in enumerate(photos, start=1):
                    url = item.get('url')
                    if url:
                        logger.info(f"Скачиваем фото #{idx}...")
                        path = await download_file(url, ".jpg")
                        temp_files.append(path)
                        logger.info(f"Грузим фото #{idx} в Gemini API...")
                        uploaded = await asyncio.to_thread(gemini_client.files.upload, file=path)
                        uploaded_files.append(uploaded)
                
            if audios:
                for idx, item in enumerate(audios, start=1):
                    url = item.get('url')
                    if url:
                        logger.info(f"Скачиваем аудио #{idx}...")
                        path = await download_file(url, ".ogg")
                        temp_files.append(path)
                        logger.info(f"Грузим аудио #{idx} в Gemini API...")
                        uploaded = await asyncio.to_thread(gemini_client.files.upload, file=path)
                        uploaded_files.append(uploaded)
                
            contents = uploaded_files.copy()
            dynamic_prompt = FIELD_PROMPT + f"\n\nGPS Координаты: {coords}"
            contents.append(dynamic_prompt)
            
            model_name = resolve_model_name()
            logger.info(f"Ждем ответа от {model_name}...")
            response = await asyncio.wait_for(
                gemini_client.aio.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.1
                    ),
                ),
                timeout=float(FIELD_GEMINI_TIMEOUT_SECONDS),
            )
            
            text_resp = (response.text or "").strip()
            # Model output may contain contact details and source text.  Keep
            # logs operationally useful without persisting that sensitive data.
            logger.info("Received Gemini JSON response (%s characters).", len(text_resp))
            if text_resp.startswith("```"):
                import re
                text_resp = re.sub(r"^```(?:json)?\n?|```$", "", text_resp).strip()
            
            parsed = json.loads(text_resp)
            if isinstance(parsed, list) and len(parsed) > 0:
                parsed = parsed[0]
                
            # Normalize Projects if returned as a list by Gemini
            if isinstance(parsed.get('Projects'), list):
                if len(parsed['Projects']) > 0:
                    parsed['Projects'] = parsed['Projects'][0]
                else:
                    parsed['Projects'] = {}

            # Координаты подставляем сами, но в двух разных порядках:
            # карта Airtable ждет "долгота,широта", а ссылка Google Maps —
            # "широта,долгота", как их и присылает Telegram. Раньше в оба
            # места писалось одно значение, и карта показывала точки не там.
            if coords:
                if 'Projects' not in parsed or not isinstance(parsed['Projects'], dict):
                    parsed['Projects'] = {}
                parsed['Projects']['Coordinates(for Map)'] = swap_coordinates(coords)
                parsed['Projects']['Location Link'] = f"https://www.google.com/maps?q={coords.replace(' ', '')}"

            # Сводка разбора. Раньше это был print для человека у консоли —
            # процесс интерактивным быть перестал, поэтому пишем в лог.
            dev_name = parsed.get('Developer', {}).get('Developer', 'Неизвестно')
            proj_name = parsed.get('Projects', {}).get('Project Name', 'Неизвестно')
            units = parsed.get('Units', [])

            logger.info(
                f"[{rec_id}] застройщик: {dev_name} | проект: {proj_name} | юнитов: {len(units)}"
            )
            for u in units:
                # Ключ без пробела — именно так поле называется в схеме парсера.
                # С пробелом здесь всегда печаталось 'No Price'.
                logger.info(
                    f"    {u.get('Unit type', 'Unknown')} | {u.get('Bedrooms')} BR | "
                    f"{u.get('Price from(USD)', 'цена не указана')} USD"
                )

            # Разбор сохраняем в саму находку. В основную базу отсюда НЕ пишем:
            # фото баннера и голосовая заметка — гипотеза, а не проверенные
            # данные. Перенос делает promote_confirmed_findings() после того,
            # как человек поставит галочку Confirmed.
            dev_data = parsed.get('Developer', {}) or {}
            proj_data = parsed.get('Projects', {}) or {}

            contacts = dev_data.get('Contacts', '')
            if isinstance(contacts, list):
                contacts = ", ".join([str(x) for x in contacts if x])

            update_fields = build_update_fields(
                priority=parsed.get('Priority', 'Средний'),
                contacts=contacts,
                final_notes=dev_data.get('Notes', '') or '',
                coords=coords,
                status='Processed',
                land_zoning=proj_data.get('Land Zoning Color'),
                handover_permits=proj_data.get('Handover Permits'),
            )
            update_fields[PARSED_JSON_FIELD] = _serialize_parsed_finding(parsed)

            # Дубль ищем по телефону с баннера: у одного застройщика мы берем
            # все проекты разом, поэтому второй баннер с тем же номером новой
            # работы не дает. Координаты для этого не годятся — баннер стоит
            # не там, где объект.
            dup_notice = None
            contact_field = update_fields.get('Contact')
            by_kind = find_matches(contact_field, all_findings, exclude_id=rec_id)
            matches = by_kind['website'] + by_kind['handle'] + by_kind['phone']
            if matches:
                incoming_project = (parsed.get('Projects') or {}).get('Project Name')
                verdict = classify_contact_match(by_kind, incoming_project,
                                                 extract_phones(contact_field))
                logger.info(
                    f"[{rec_id}] совпадений: сайт={len(by_kind['website'])} "
                    f"аккаунт={len(by_kind['handle'])} телефон={len(by_kind['phone'])} "
                    f"-> {verdict}"
                )
                if field_exists('Field Staging', 'Possible Duplicate Of'):
                    update_fields['Possible Duplicate Of'] = [m['id'] for m in matches[:5]]
                if field_exists('Field Staging', 'Duplicate Reason'):
                    update_fields['Duplicate Reason'] = describe_contact_match(
                        by_kind, contact_field, incoming_project)
                dup_notice = build_contact_notice(by_kind, contact_field, incoming_project)

            await asyncio.to_thread(_staging_table().update, rec_id, update_fields)
            logger.info(
                f"Запись {rec_id} разобрана (Status=Processed). "
                f"Для переноса в основную базу поставьте галочку Confirmed."
            )

            if dup_notice:
                await notify_lister(fields.get('Telegram Chat ID'), dup_notice)

        except Exception as e:
            logger.error(f"Ошибка при обработке: {e}")
            try:
                await asyncio.to_thread(_staging_table().update, rec_id, {'Status': 'Error', 'Notes': f"SYSTEM ERROR: {str(e)}"})
            except Exception as update_exc:
                logger.error(
                    "Could not persist error status for staging record %s: %s",
                    rec_id,
                    update_exc,
                )
            
        finally:
            # Cleanup temp files
            for p in temp_files:
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError as cleanup_exc:
                        logger.warning("Could not remove temporary attachment %s: %s", p, cleanup_exc)
            for f in uploaded_files:
                try:
                    await asyncio.to_thread(gemini_client.files.delete, name=f.name)
                except Exception as cleanup_exc:
                    logger.warning(
                        "Could not delete temporary Gemini file for staging record %s: %s",
                        rec_id,
                        cleanup_exc,
                    )

from app.single_instance import acquire

if __name__ == '__main__':
    acquire('field_processor')
    async def main_loop():
        logger.info("Запуск фонового обработчика (проверка каждые 30 секунд)...")
        while True:
            try:
                await process_staging_records()
            except Exception as e:
                logger.error(f"Сбой в главном цикле: {e}")
            await asyncio.sleep(30)
            
    asyncio.run(main_loop())
