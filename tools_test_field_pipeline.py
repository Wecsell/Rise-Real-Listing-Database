"""
tools_test_field_pipeline.py
============================
Автономный сквозной тест конвейера полевого обработчика (field_processor.py).

Тестовый сценарий:
1. Создает временную тестовую запись в Field Staging со статусом 'New'.
2. Запускает функцию parse_new_findings() из field_processor.py.
3. Проверяет, что запись успешно разобрана, статусом стал 'Processed', заполнено поле Contact и Parsed JSON.
4. Устанавливает Confirmed = True и запускает promote_confirmed_findings().
5. Проверяет создание/связывание с проектом в основной базе.
6. В конце удаляет тестовые записи для чистоты эксперимента.
"""

import sys
import os
import asyncio
import logging
from dotenv import load_dotenv
from pyairtable import Api

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

# На Windows 3.14 фоновые процессы требовательны к пути к OpenSSL DLL (_ssl)
if sys.platform == 'win32':
    py_dir = os.path.dirname(sys.executable)
    dll_dir = os.path.join(py_dir, 'DLLs')
    if os.path.exists(dll_dir):
        os.environ['PATH'] = dll_dir + os.pathsep + os.environ.get('PATH', '')
        try:
            os.add_dll_directory(dll_dir)
        except Exception:
            pass

load_dotenv(override=True)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger("TestFieldPipeline")

TOKEN = os.environ.get('AIRTABLE_TOKEN')
BASE_ID = os.environ.get('AIRTABLE_BASE_ID')
api = Api(TOKEN)
base = api.base(BASE_ID)
staging_table = base.table('Field Staging')

async def run_pipeline_test():
    log.info("Starting Field Processor Pipeline Test...")

    # 1. Создаем тестовую запись в Field Staging
    test_fields = {
        'Status': 'New',
        'Coordinates': '-8.6384, 115.1038',
        'Notes': 'Test billboard entry: CEMAGI ROCK VILLAS developer CEMAGI contact +6281338889995',
        'Submitted By': 'Test Suite Runner Pipeline',
    }

    log.info("1. Creating synthetic test record in Field Staging...")
    rec = staging_table.create(test_fields)
    rec_id = rec['id']
    log.info(f"Created test record: {rec_id}")

    try:
        # 2. Запускаем parse_new_findings
        from field_processor import parse_new_findings, promote_confirmed_findings
        log.info("2. Running parse_new_findings()...")
        await parse_new_findings()

        # 3. Проверяем результат обработки
        updated_rec = staging_table.get(rec_id)
        u_fields = updated_rec.get('fields', {})
        log.info(f"Updated record Status: {u_fields.get('Status')}")
        log.info(f"Updated Contact: {u_fields.get('Contact')}")
        log.info(f"Updated Priority: {u_fields.get('Priority')}")
        
        assert u_fields.get('Status') == 'Processed', f"Expected Processed status, got {u_fields.get('Status')}"
        assert 'Contact' in u_fields or 'Parsed JSON' in u_fields, "Expected Contact or Parsed JSON populated"

        log.info("✅ Step 1.1 (Parse New Findings) PASSED!")

        # 4. Проверяем promote_confirmed_findings
        log.info("3. Setting Confirmed = True and testing promote_confirmed_findings()...")
        staging_table.update(rec_id, {'Confirmed': True})
        await promote_confirmed_findings()

        promoted_rec = staging_table.get(rec_id)
        p_fields = promoted_rec.get('fields', {})
        log.info(f"Promoted Record Linked Project: {p_fields.get('Project')}")
        log.info(f"Promoted Record Linked Developer: {p_fields.get('Developer')}")
        assert 'Project' in p_fields or 'Developer' in p_fields, "Expected Project or Developer linked after promotion"

        log.info("✅ Step 1.2 (Promote Confirmed Findings) PASSED!")

    except Exception as e:
        log.error(f"❌ Test failed: {e}")
        raise e
    finally:
        # Clean up test record
        log.info(f"Cleaning up test record {rec_id}...")
        try:
            staging_table.delete(rec_id)
            log.info("Cleanup complete.")
        except Exception as ce:
            log.warning(f"Cleanup error: {ce}")

if __name__ == '__main__':
    asyncio.run(run_pipeline_test())
