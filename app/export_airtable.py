import asyncio
import asyncpg
import csv
import os
from datetime import datetime

# Внутри Docker контейнера используем имя сервиса 'postgres', снаружи — 'localhost'
DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://postgres:rise_secure_pass_2026@postgres:5432/rise')

async def export_data():
    print("Connecting to Postgres database...")
    try:
        conn = await asyncpg.connect(DATABASE_URL)
    except Exception as e:
        print(f"Error connecting to database: {e}")
        return

    # Запрашиваем все извлечённые данные
    rows = await conn.fetch("""
        SELECT e.id, e.message_id, e.project_recid, e.object_guess, 
               e.confidence, e.slot, e.url_status, e.why, e.created_at, 
               m.text, m.chat_id
        FROM extractions e
        LEFT JOIN messages m ON e.message_id = m.id
        ORDER BY e.created_at DESC
    """)

    print(f"Found {len(rows)} extractions in database.")

    if len(rows) == 0:
        print("⚠️ No data to export yet. The parser needs time to scan chats.")
        await conn.close()
        return

    export_file = f"/data/airtable_export_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    
    headers = [
        'Project Name', 'Unit Type', 'Source (Slot)', 'URL Status',
        'Confidence', 'Details', 'Source Message', 'Extracted At'
    ]

    with open(export_file, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        
        for r in rows:
            source_text = (r['text'] or '')[:300].replace('\n', ' ')
            writer.writerow([
                r['project_recid'],
                r['object_guess'],
                r['slot'],
                r['url_status'],
                r['confidence'],
                r['why'],
                source_text,
                r['created_at'].strftime('%Y-%m-%d %H:%M:%S')
            ])

    print(f"✅ Exported {len(rows)} rows to: {export_file}")
    
    # Вывести первые 10 строк на экран для быстрого просмотра
    print("\n--- Preview (first 10 rows) ---")
    for r in rows[:10]:
        print(f"  📍 {r['project_recid']} | {r['object_guess']} | {r['why'][:80]}")
    
    await conn.close()

if __name__ == '__main__':
    asyncio.run(export_data())
