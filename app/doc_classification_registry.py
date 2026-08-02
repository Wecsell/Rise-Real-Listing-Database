"""
Реестр классификаций документов в Postgres (implementation_plan.md, Э2,
"Не сделано в Э2" из walkthrough 01.08.2026).

Зачем. classify_document() в app/doc_router.py опознаёт файл по имени бесплатно,
регуляркой. Но часть файлов застройщика не опознаётся по имени вовсе (папка
"unknown" в route_files_for_gaps) - для них план прямо требует fallback: открыть
в пределах бюджета и классифицировать МОДЕЛЬЮ. Без реестра эта дорогая
классификация считалась бы заново при каждом прогоне по тому же проекту -
план требует, чтобы она "заполнялась один раз, переиспользовалась всеми
проектами" (раздел Э2).

Ключ реестра - file_id Google Drive, а не (project, file_id): один и тот же
файл физически не может относиться к двум проектам, а план явно говорит про
переиспользование "всеми проектами", а не только текущим.

Тот же паттерн пула, что app/database.py: модуль работает без БД (pool is None)
не падая - реестр это ускорение, а не обязательное условие пайплайна.
"""
import logging
from typing import Optional

import app.database as database

logger = logging.getLogger("DocClassificationRegistry")

# ВАЖНО: обращаться к database.pool через сам модуль, а не через
# "from app.database import pool" - init_db() присваивает пул уже после
# импорта, и локальная копия ссылки навсегда осталась бы None (тот же класс
# бага, что уже ловился в проекте на захардкоженных списках схемы).


async def get_cached_classification(file_id: str) -> Optional[str]:
    """Тип документа из реестра, если файл уже классифицировался. None - нет записи или нет БД."""
    if not database.pool or not file_id:
        return None
    try:
        async with database.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT doc_type FROM document_classifications WHERE file_id = $1",
                file_id,
            )
    except Exception as e:
        logger.error(f"Не удалось прочитать классификацию файла {file_id}: {e}")
        return None


async def save_classification(
    file_id: str,
    doc_type: str,
    classified_by: str,
    model_used: Optional[str] = None,
) -> None:
    """
    Записывает результат классификации. ON CONFLICT DO UPDATE - переклассификация
    допустима (например ручная правка ошибочного ответа модели), а не запрет
    на повторную запись того же file_id.
    """
    if not database.pool or not file_id or not doc_type:
        return
    try:
        async with database.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO document_classifications (file_id, doc_type, classified_by, model_used)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (file_id) DO UPDATE
                    SET doc_type = EXCLUDED.doc_type,
                        classified_by = EXCLUDED.classified_by,
                        model_used = EXCLUDED.model_used,
                        created_at = NOW()
                """,
                file_id, doc_type, classified_by, model_used,
            )
    except Exception as e:
        logger.error(f"Не удалось сохранить классификацию файла {file_id}: {e}")
