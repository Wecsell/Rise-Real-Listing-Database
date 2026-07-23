import asyncio
import asyncpg
import csv
import json
import os
from datetime import datetime

DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://postgres:rise_secure_pass_2026@localhost:5432/rise')

async def export_data():
    print("Connecting to Postgres database...")
    try:
        conn = await asyncpg.connect(DATABASE_URL)
    except Exception as e:
        print(f"Error connecting to database: {e}")
        return

    # Запрашиваем извлеченные данные
    rows = await conn.fetch("""
        SELECT e.id, e.message_id, e.project_recid, e.object_guess, e.confidence, e.why, e.created_at, m.text, m.chat_id
        FROM extractions e
        JOIN messages m ON e.message_id = m.id
        ORDER BY e.created_at DESC
    """)

    print(f"Found {len(rows)} extractions in database.")

    # Файл 1: Для таблицы Projects в Airtable
    projects_file = f"airtable_projects_export_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    with open(projects_file, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(['Project Name', 'Unit type', 'Confidence', 'Extraction Reason', 'Source Message Text', 'Created At'])
        for r in rows:
            writer.writerow([
                r['project_recid'],
                r['object_guess'],
                r['confidence'],
                r['why'],
                r['text'][:200].replace('\n', ' '),
                r['created_at'].strftime('%Y-%m-%d %H:%M:%S')
            ])

    print(f"✅ Success! Exported Airtable CSV: {projects_file}")
    await conn.close()

if __name__ == '__main__':
    asyncio.run(export_data())
