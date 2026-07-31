import sys
import os

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from app.airtable_client import get_base

base = get_base()
print("Base:", base)

try:
    recs_with = base.table('Projects').all(fields=['Project Name', 'Developer', 'District', 'Aliases'])
    print(f"With fields parameter: {len(recs_with)} records")
    for r in recs_with[:5]:
        print("  -", r['fields'])
except Exception as e:
    print("Error with fields:", e)

try:
    recs_without = base.table('Projects').all()
    print(f"Without fields parameter: {len(recs_without)} records")
    for r in recs_without[:5]:
        print("  -", r['fields'].get('Project Name'))
except Exception as e:
    print("Error without fields:", e)
