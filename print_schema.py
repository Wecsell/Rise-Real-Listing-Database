import json
data = json.load(open('schema.json', encoding='utf-16'))
with open('out_repr.txt', 'w', encoding='utf-8') as out:
    for t in data.get('tables', []):
        if t['name'] in ('Projects', 'Units'):
            out.write(f"Table {t['name']}:\n")
            for f in t.get('fields', []):
                if any(x in f['name'] for x in ['Kit', 'm2', 'm', 'Price', 'Area']):
                    out.write(f"{repr(f['name'])}\n")
