"""
Собственная карта проектов взамен штатного Map Starter Kit в Airtable Interfaces.

Почему не расширение Airtable: у него нет режима «поставить точку по числам» —
единственный вход, Address field, уходит в геокодер Mapbox. На координатную
строку Mapbox отвечает обратным геокодированием и возвращает мельчайший
известный ему объект — в Seseh и Nusa Dua это сама деревня, а не участок.
Булавка садится в её центроид: район верен, место — нет. См. память
airtable-map-is-a-geocoder.md.

Этот скрипт читает Projects как есть (поле Coordinates(for Map) в каноне
«долгота, широта», трогать не нужно — см. tools_fix_coordinates.py) и рисует
точки в Leaflet поверх тайлов OpenStreetMap. Никакого геокодирования: числа
идут прямо в координаты маркера, точность абсолютная.

Результат — один самодостаточный HTML-файл (Leaflet и его CSS с CDN, тайлы
OSM тоже внешние — открывать нужно с доступом в интернет). Его нужно
разместить на любом статическом хостинге и вставить URL в элемент Embed
интерфейса Airtable. Сам файл секретов не содержит: только Project Name,
Developer, Location и координаты, которые и так видны на карте.

Запуск: python tools_build_projects_map.py [--out FILE]
"""
import json
import os
import sys
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(override=True)

TOKEN = os.environ['AIRTABLE_TOKEN']
BASE = os.environ['AIRTABLE_BASE_ID']
HEADERS = {'Authorization': f'Bearer {TOKEN}'}

OUT = 'projects_map.html'
if '--out' in sys.argv:
    OUT = sys.argv[sys.argv.index('--out') + 1]


def fetch(table):
    records, offset = [], None
    while True:
        params = {'pageSize': '100'}
        if offset:
            params['offset'] = offset
        url = f'https://api.airtable.com/v0/{BASE}/' + urllib.parse.quote(table) + '?' + urllib.parse.urlencode(params)
        data = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=20))
        records += data['records']
        offset = data.get('offset')
        if not offset:
            return records


def parse_coords(raw):
    """Канон поля — 'долгота, широта'. Возвращает (lat, lng) для Leaflet или None."""
    if not raw or not isinstance(raw, str):
        return None
    parts = [p.strip() for p in raw.split(',')]
    if len(parts) != 2:
        return None
    try:
        lng, lat = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    return lat, lng


def main():
    projects = fetch('Projects')

    points = []
    skipped = 0
    for rec in projects:
        f = rec['fields']
        coords = parse_coords(f.get('Coordinates(for Map)'))
        if not coords:
            skipped += 1
            continue
        lat, lng = coords
        points.append({
            'name': f.get('Project Name', '(без имени)'),
            'lat': lat,
            'lng': lng,
            'location': f.get('Location', ''),
            'link': f.get('Location Link', ''),
        })

    print(f'точек на карте: {len(points)}   пропущено (нет/непарсится координата): {skipped}')

    html = TEMPLATE.replace('__DATA__', json.dumps(points, ensure_ascii=False))
    with open(OUT, 'w', encoding='utf-8') as fh:
        fh.write(html)
    print(f'записано: {OUT}')
    return 0


TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Projects Map</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<style>
  html, body, #map { height: 100%; margin: 0; }
  .pin-label { font: 12px/1.3 -apple-system, Segoe UI, sans-serif; }
</style>
</head>
<body>
<div id="map"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
  const points = __DATA__;

  const map = L.map('map');
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap contributors'
  }).addTo(map);

  const markers = [];
  points.forEach(p => {
    const m = L.marker([p.lat, p.lng]).addTo(map);
    const link = p.link ? `<br><a href="${p.link}" target="_blank" rel="noopener">Google Maps</a>` : '';
    m.bindPopup(`<b>${p.name}</b><br>${p.location || ''}${link}`);
    markers.push(m);
  });

  if (markers.length) {
    map.fitBounds(L.featureGroup(markers).getBounds().pad(0.15));
  } else {
    map.setView([-8.6, 115.2], 10); // Бали по умолчанию, если точек нет
  }
</script>
</body>
</html>
"""

if __name__ == '__main__':
    sys.exit(main())
