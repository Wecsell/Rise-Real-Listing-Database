import os
import json
import asyncio
import logging
from typing import Optional, Dict, Any
import pypdf
from google import genai
from google.genai import types

from app import content_cache
from app.gemini_parser import (
    client,
    SYSTEM_PROMPT,
    ParsedExtraction,
    parse_message,
    resolve_model_name,
)

logger = logging.getLogger("DocParser")

def extract_text_from_pdf(pdf_path: str) -> str:
    """Извлекает текстовое содержимое из PDF файла."""
    extracted_text = []
    try:
        reader = pypdf.PdfReader(pdf_path)
        for page_num, page in enumerate(reader.pages, start=1):
            text = page.extract_text()
            if text:
                extracted_text.append(f"--- Страница {page_num} ---\n{text}")
    except Exception as e:
        logger.error(f" Ошибка чтения PDF файла {pdf_path}: {e}")
    return "\n\n".join(extracted_text)

async def parse_pdf_document(pdf_path: str, chat_title: Optional[str] = None) -> Dict[str, Any]:
    """
    Выполняет анализ PDF-документа (Dev Kit / Брошюра / Шахматка).
    Поддерживает обработку крупных PDF (>50 МБ) через фоллбэк извлечения текста.
    """
    if not os.path.exists(pdf_path):
        return {"is_relevant": False, "error": f"Файл не найден: {pdf_path}"}
        
    file_size_mb = os.path.getsize(pdf_path) / (1024 * 1024)
    logger.info(f"Начинаем анализ PDF-документа: {pdf_path} (Размер: {file_size_mb:.2f} МБ)")
    
    raw_text = extract_text_from_pdf(pdf_path)
    
    # 1. Если в PDF содержался текстовый слой (достаточной длины)
    if len(raw_text.strip()) > 100:
        logger.info(f"Извлечен текстовый слой из PDF ({len(raw_text)} символов). Запускаем Gemini Parser...")
        max_chars = 40000
        truncated_text = raw_text[:max_chars]
        return await parse_message(truncated_text, chat_title=chat_title)

    # 2. Если текстовый слой мал, но файл больше 50 МБ (лимит Gemini Files API)
    if file_size_mb > 50.0:
        logger.warning(f"⚠️ PDF файл {pdf_path} весит {file_size_mb:.2f} МБ, что превышает лимит Gemini API (50 МБ). Применяем фоллбэк сбора доступных метаданных.")
        if len(raw_text.strip()) > 0:
            return await parse_message(raw_text, chat_title=chat_title)
        return {
            "is_relevant": True,
            "Gaps": [f"PDF Dev Kit exceeds Gemini 50MB limit ({file_size_mb:.1f} MB) and lacks text layer"],
            "reason": "PDF file too large for direct vision API"
        }

    # 3. Файл <= 50 МБ и без текстового слоя — загружаем в Gemini Files API для графического анализа
    logger.info("Текстовый слой пуст. Загружаем PDF в Gemini Files API для графического анализа...")
    if not client:
        return {"is_relevant": False, "error": "Gemini API client not initialized"}

    # Тот же PDF мог прийти повторно (пересланный Dev Kit, повторный скан
    # чата) — графический разбор через Files API самый дорогой вызов в
    # проекте, проверяем кэш по содержимому файла до загрузки.
    file_hash = await asyncio.to_thread(content_cache.hash_file, pdf_path)
    cached = await asyncio.to_thread(content_cache.get, file_hash)
    if cached is not None:
        logger.info(f"💾 Cache hit for PDF {os.path.basename(pdf_path)} (hash={file_hash[:12]}...), skipping upload")
        return cached
    logger.info(f"Cache miss for PDF hash={file_hash[:12]}..., uploading to Gemini Files API")

    try:
        uploaded_file = await asyncio.to_thread(client.files.upload, file=pdf_path)
        
        dynamic_prompt = SYSTEM_PROMPT + "\n\nВНИМАНИЕ: Тебе передан PDF-документ (Dev Kit / Презентация / Шахматка объекта). Внимательно изучи все страницы, таблицы и графику, извлеки данные по проекту и всем юнитам."
        if chat_title:
            dynamic_prompt += f"\nКОНТЕКСТ: Файл получен из чата '{chat_title}'."
            
        model_name = resolve_model_name()
        response = await client.aio.models.generate_content(
            model=model_name,
            contents=[uploaded_file, "Извлеки все данные по проекту и юнитам из этого PDF-файла."],
            config=types.GenerateContentConfig(
                system_instruction=dynamic_prompt,
                response_mime_type="application/json",
                response_schema=ParsedExtraction,
                temperature=0.1
            )
        )
        
        text_resp = response.text.strip()
        parsed_json = json.loads(text_resp)

        if content_cache.is_cacheable(parsed_json):
            await asyncio.to_thread(content_cache.put, file_hash, 'pdf_graphic', parsed_json)

        return parsed_json

    except Exception as e:
        logger.error(f"Ошибка мультимодального анализа PDF: {e}")
        return {"is_relevant": False, "error": str(e)}

