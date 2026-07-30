import json

with open('dump_cleaned.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

for i, d in enumerate(data):
    if not d.get('is_relevant'): continue
    pn = str(d.get('Projects', {}).get('Project Name', '')).lower()
    dev = str(d.get('Developer', {}).get('Developer', '')).lower()
    if 'rise' in pn or 'rise' in dev or 're ' in pn or 're villas' in pn or 're ' in dev:
        print(f'Match in record {i}:')
        print(f'  Developer: {d.get("Developer")}')
        print(f'  Project: {d.get("Projects")}')
