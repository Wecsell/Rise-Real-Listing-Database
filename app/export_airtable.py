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

    # Запрашиваем сообщения и логи извлечений
    rows = await conn.fetch("""
        SELECT e.id, e.message_id, e.project_recid, e.object_guess, e.confidence, e.why, e.created_at, m.text, m.chat_id
        FROM extractions e
        JOIN messages m ON e.message_id = m.id
        ORDER BY e.created_at DESC
    """)

    print(f"Found {len(rows)} extractions in database.")

    # Файл 1: Полный CSV со всеми колонками Airtable
    export_file = f"airtable_full_export_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    
    headers = [
        'Project Name', 'Developer Name', 'Location Area', 'Property Type', 
        'Construction Stage', 'Completion Date', 'Ownership Type', 'Leasehold Years',
        'Yield ROI %', 'Price From (USD)', 'Offers / Discounts', 'Coordinates',
        'Unit ID', 'Unit Type', 'Bedrooms', 'Bathrooms', 'Area (m²)', 'Price (USD)',
        'Availability', 'View Type', 'Amenities', 'Extraction Confidence', 'Source Text'
    ]

    with open(export_file, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        
        for r in rows:
            writer.writerow([
                r['project_recid'],               # Project Name
                '',                               # Developer Name
                '',                               # Location Area
                r['object_guess'],                # Property Type / Unit Type
                '',                               # Construction Stage
                '',                               # Completion Date
                '',                               # Ownership Type
                '',                               # Leasehold Years
                '',                               # Yield ROI %
                '',                               # Price From
                '',                               # Offers
                '',                               # Coordinates
                '',                               # Unit ID
                r['object_guess'],                # Unit Type
                '',                               # Bedrooms
                '',                               # Bathrooms
                '',                               # Area
                '',                               # Price
                'On sale',                        # Availability
                '',                               # View Type
                '',                               # Amenities
                r['confidence'],                  # Confidence
                r['text'][:300].replace('\n', ' ')# Source Text
            ])

    print(f"✅ Success! Full Airtable CSV exported to: {export_file}")
    await conn.close()

if __name__ == '__main__':
    asyncio.run(export_data())
