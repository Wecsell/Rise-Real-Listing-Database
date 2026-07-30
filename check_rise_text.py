import json

with open('dump.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

for i, d in enumerate(data):
    if not d.get('is_relevant'): continue
    pn = str(d.get('Projects', {}).get('Project Name', '')).lower()
    dev = str(d.get('Developer', {}).get('Developer', '')).lower()
    if 're ' in pn or 're villas' in pn or 're ' in dev or 'rise' in pn:
        print(f'Original Text for record {i}:')
        print(d.get('original_text')[:400])
        print("===")
