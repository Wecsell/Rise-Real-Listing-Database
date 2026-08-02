"""
Листинг проектов застройщика Bali Baza в Airtable (Developer/Projects/Units)
плюс зеркалирование рендеров на Google Drive владельца (Э6).

Источники (все прочитаны живьём 02.08.2026, ничего не захардкожено «на глаз»):
  - группа Telegram "Baza - RiseReal" в папке RR Groups (24 сообщения);
  - партнёрский портал https://bali-baza-partners.vercel.app/ - карточки
    5 актуальных проектов со всеми ссылками на материалы;
  - шахматка Baza Kedungu (публичный Google Sheet) - 58 юнитов поимённо.

Чего в источниках НЕТ и что поэтому не заполняется:
  - шахматка The Heights выдана поимённо, недоступна ни публично (401), ни
    нашему OAuth (404) - юниты по ней не создаются, уходит в Gaps;
  - у Gate 11 и Sunset Village (оба сданы) поюнитных данных нет вовсе.

Запуск: python tools_list_baza_projects.py            - только план
        python tools_list_baza_projects.py --apply    - записать в Airtable
        python tools_list_baza_projects.py --apply --mirror  - плюс зеркало рендеров
"""
import argparse
import asyncio
import csv
import io
import re
import sys
import urllib.request
from collections import defaultdict

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from dotenv import load_dotenv
load_dotenv(override=True)

from app.airtable_client import (get_base, get_select_options, init_cache_async,
                                 robust_airtable_op, upsert_project, upsert_unit)

DEVELOPER_NAME = "Bali Baza"

# Контакты собраны из чата, партнёрского портала и вики The Heights.
DEVELOPER_CONTACTS = (
    "+62 81139908903, @balibazapartners, @balibazaagents, "
    "@agent_bali_baza, +62 89505211764"
)

KEDUNGU_MATRIX_SHEET = "1TWbIAHCWka3ChmSxnl_n1brWpvHWygSgQygDn7SoKMI"

# Найдена 02.08.2026: закреплённое сообщение в чате Baza - RiseReal и вики
# theheights.io/wiki/eng ведут на ту же таблицу, что и портал - просто нужен
# был другой лист. У Google Sheets 3 вкладки (gid из htmlview, публичный CSV
# export отдаёт 401, а gviz - нет): калькулятор цены (демо на Type 6),
# планировка по этажам, и "Full unit registry" - 105 юнитов поимённо со
# статусом Sold/Available. Именно третий лист и был нужен.
HEIGHTS_MATRIX_SHEET = "1a-yy-dG02WZqJxy3J8OAFQpWbjNZD2QEEmjzfOw7QZQ"
HEIGHTS_REGISTRY_GID = "2010370332"
HEIGHTS_PRICE_PER_M2 = 3500  # текущая цена по чату/калькулятору того же файла

# Type N -> (Unit type для Airtable, спальни). Спальни проставлены только там,
# где есть прямое подтверждение (Type 9 назван "Extended Residence (1BR)" в
# папке рендеров; Studio по определению без отдельной спальни). Type 1/2
# ("Two-level") и редкие 3/4/10 - без спален: угадывать план прямо запрещает.
HEIGHTS_TYPE_MAP = {
    "Type 1": ("Apartment", None),
    "Type 2": ("Apartment", None),
    "Type 3": ("Penthouse", None),
    "Type 4": ("Apartment", None),
    "Type 5": ("Studio", None),
    "Type 6": ("Studio", None),
    "Type 8": ("Studio", None),
    "Type 9": ("Apartment", 1),
    "Type 10": ("Apartment", None),
}

# Тип из шахматки -> Units.Unit type (живой селект: Villa/Apartment/Loft/
# Townhouse/Studio/Penthouse). Honeymoon и Presidential suite - по сути
# апартаменты (подтверждено владельцем 02.08.2026).
UNIT_TYPE_MAP = {
    "studio": "Studio",
    "apartment": "Apartment",
    "apartment garden": "Apartment",
    "honeymoon suite": "Apartment",
    "presidential suite": "Apartment",
    "villa": "Villa",
    "townhouse": "Townhouse",
    "penthouse": "Penthouse",
}

# Status шахматки -> Units.Availability (живой селект: On sale/Blocked/Sold)
UNIT_STATUS_MAP = {"available": "On sale", "reserved": "Blocked", "sold": "Sold"}

# Папки рендеров/фото с партнёрского портала: проект -> id папки Drive.
RENDER_FOLDERS = {
    "Baza Kedungu": "1K7QStLuWtaGDpHTjNggffsfAUz_WvsCV",
    "Origins": "1C3OVAWl_S3AMbAx3TNoi9lJtLw01Mu7-",
    "The Heights": "19EWkFfVYJZOE4X15fBb8-3q2BSCoRcxG",
    "Gate 11": "1JUe0EBFWbLEU4HQdOlth4PENL1HJeYoN",
    "Sunset Village": "14v96bPkeJplRm-U0WF_-7cWUSvoR8Yer",
}

PROJECTS = [
    {
        "Project Name": "Baza Kedungu",
        # В базе проект уже заведён под коротким именем 'Kedungu'. Сопоставляем
        # по нему ЯВНО: fuzzy_match_project('Baza Kedungu') даёт score 0.000 и
        # создал бы дубль (проверено на живых данных 02.08.2026).
        "_existing_name": "Kedungu",
        "District": "Kedungu",
        "Property Type": ["Apartment"],
        "Construction stage": "Finishing",
        "Ownership Type": "Leasehold",
        "Lease Term (years)": 30,
        # Поле в базе — singleLineText, а не число (проверено 02.08.2026):
        # числом Airtable отвергает всю запись проекта с 422.
        "Extension Term (years)": "30",
        "Handover Date": "2027-06-30",
        "Handover Permits": "PBG",
        "Total Units": 58,
        "Downpayment": 0.05,
        "Availability Chart": f"https://docs.google.com/spreadsheets/d/{KEDUNGU_MATRIX_SHEET}/edit?gid=849122232",
        "Link to Developer’s Kit (Rus)": "https://drive.google.com/drive/folders/1CYb5VUvG2_ba_H8h9Q4JPHUHq7FWyOjT",
        "Link to Developer’s Kit (Eng)": "https://drive.google.com/drive/folders/1nzulfJcek4KM7OcJQm4yHESltHCRCbxn",
        "Location Link": "https://maps.app.goo.gl/GmarQubT1qfEuk3J7",
        "Special Conditions": (
            "Дизайнерский апарт-комплекс в 3 минутах пешком от океана, под управлением "
            "международного отельного оператора. Подписан LOI с Dusit Hotels & Resorts. "
            "Доходность ~14% годовых. Рассрочка с ПВ от 5%. "
            "Гарантия дохода 9% для Honeymoon Suite."
        ),
        "_units_from_matrix": "kedungu",
    },
    {
        "Project Name": "Origins",
        "_existing_name": "Origins",
        "District": "Nuanu",
        "Property Type": ["Villa"],
        "Construction stage": "Structure",
        "Ownership Type": "Leasehold",
        "Lease Term (years)": 25,
        "Extension Term (years)": "20+10",
        "Handover Date": "2027-06-30",
        "Handover Permits": "PBG",
        "Total Units": 18,
        "Price From (USD)": 537000,
        "Link to Developer’s Kit (Rus)": "https://drive.google.com/drive/folders/1Sab_ga463aU5Z1odDX-xO9MiTIr_HlQP",
        "Link to Developer’s Kit (Eng)": "https://drive.google.com/drive/folders/1_3aDSSLruLFtVcBcEwHh8jVD2bjTizmV",
        "Location Link": "https://maps.app.goo.gl/6zLVcgaLwp5Rp4wV9",
        "Special Conditions": (
            "Премиальные бутик-виллы в Nuanu City под управлением международного "
            "отельного оператора. Доходность до 16% годовых. "
            "Лизхолд 25+20+10 лет. Типы: Villa A (3 спальни), Villa B (2 спальни), "
            "гостевой дом и комната персонала."
        ),
        # Типология из структуры папок рендеров застройщика. Цен по типам в
        # доступных материалах нет - в карточке проекта стоит только «от».
        "_units": [
            {"Unit Number": "VILLA-A", "Unit type": "Villa", "Bedrooms": 3,
             "Availability": "On sale", "Area": "Nuanu"},
            {"Unit Number": "VILLA-B", "Unit type": "Villa", "Bedrooms": 2,
             "Availability": "On sale", "Area": "Nuanu"},
        ],
        "_gaps": [
            "поюнитная шахматка Origins не найдена в материалах",
            "цены по типам вилл неизвестны - в портале только «от $537 000»",
        ],
    },
    {
        "Project Name": "The Heights",
        "_existing_name": "The Heights",
        "District": "Seseh",
        # 'Penthouse' добавлена в живой селект Projects.Property Type 02.08.2026
        # (решение владельца) - у The Heights реально есть Type 3 (157м², sold).
        # Раньше её здесь не было вовсе (только Villa/Apartment/Studio/
        # Townhouse), и запись с ней ронялась 422 - используйте typecast=True
        # при первой записи нового значения селекта, обычная запись такую
        # опцию не создаёт и отвергает всю карточку целиком.
        "Property Type": ["Apartment", "Studio", "Penthouse"],
        "Construction stage": "Foundation",
        "Ownership Type": "Leasehold",
        "Lease Term (years)": 30,
        # Поле в базе — singleLineText, а не число (проверено 02.08.2026):
        # числом Airtable отвергает всю запись проекта с 422.
        "Extension Term (years)": "30",
        "Handover Date": "2027-03-31",
        "Handover Permits": "PBG",
        "Total Units": 105,
        "Downpayment": 0.3,
        # Закреплённое сообщение в чате (приоритетный источник, решение
        # владельца 02.08.2026) и вики theheights.io/wiki/eng ведут на ту же
        # таблицу - лист "Full unit registry" (gid=2010370332), а не на
        # калькулятор цены (gid=1499658778), на который смотрели раньше.
        "Availability Chart": f"https://docs.google.com/spreadsheets/d/{HEIGHTS_MATRIX_SHEET}/edit?gid={HEIGHTS_REGISTRY_GID}",
        "Link to Developer’s Kit (Rus)": "https://theheights.io/wiki",
        "Link to Developer’s Kit (Eng)": "https://theheights.io/wiki/eng",
        "Location Link": "https://maps.app.goo.gl/bozuiKu7FBR9VhpZ8",
        "Installment Notes": (
            "30% — первый взнос, 10% — на сдаче объекта, 5% — ежемесячно в течение 12 месяцев"
        ),
        "Special Conditions": (
            "Оптимальный набор наиболее востребованных функций в одном пространстве с единым "
            "архитектурным кодом. Доходность до 16,2% годовых. Текущая цена $3 500/м², "
            "с рассрочкой $3 550/м². Эксклюзивные продажи через Bali Baza. Застройщик по "
            "решению владельца 02.08.2026 зафиксирован как Bali Baza; уточнение роли "
            "(девелопер/эксклюзивный продавец) - на усмотрение менеджера-сейлза при запросе клиента."
        ),
        "_units_from_matrix": "heights",
        "_gaps": [
            "цена по типам 3/4/10 и спальни Type 1/2 (Two-level) не заведены - "
            "в реестре есть только площадь, план квартиры не разбирался",
        ],
    },
    {
        "Project Name": "Gate 11",
        "District": "Kerobokan",
        "Property Type": ["Apartment"],
        "Construction stage": "Completed",
        "Price From (USD)": 99000,
        "Link to Developer’s Kit (Rus)": "https://drive.google.com/drive/folders/1CIwTB-btzC8UrqRqZbg4HIje8KCkEq0B",
        "Location Link": "https://maps.app.goo.gl/bRnGhNFvjc6sX58Y6",
        "Special Conditions": (
            "Сдан, доход с первого дня. Осталось 2 апартамента от $99k. "
            "Типы: студии и мезонины, три варианта отделки (Main, White, Bright)."
        ),
        # Area у юнитов НЕ ставим: в селекте Units.Area значения 'Kerobokan'
        # нет (оно есть только в Projects.District) - списки этих двух полей
        # в базе разные. Молча уехало бы в пустоту при записи.
        "_units": [
            {"Unit Number": "STUDIO", "Unit type": "Studio", "Bedrooms": 1,
             "Availability": "On sale"},
            {"Unit Number": "MEZZANINE", "Unit type": "Apartment", "Bedrooms": 1,
             "Availability": "On sale"},
        ],
        "_gaps": [
            "поюнитной шахматки Gate 11 в материалах нет, типология взята из папок фото",
            "цены по типам неизвестны - в портале только «осталось 2 апартамента по $99k»",
            "район юнитов не заполнен: в селекте Units.Area нет значения 'Kerobokan'",
        ],
    },
    {
        "Project Name": "Sunset Village",
        "District": "Kerobokan",
        "Property Type": ["Villa"],
        "Construction stage": "Completed",
        "Price From (USD)": 130000,
        "Link to Developer’s Kit (Rus)": "https://drive.google.com/drive/folders/1KoymPpnRzmGu1yeNTz2XX5QQS36Wp-0d",
        "Location Link": "https://maps.app.goo.gl/vFLJnAQBAA1jG3nFA",
        "Special Conditions": (
            "Сдан. Перепродажа: виллы 2BR с бассейном от $130k. Комиссия агенту $5 000. "
            "В материалах есть корпуса B3A, B8, B10, C3A, C10, D2 (2BR и 3BR)."
        ),
        "_units": [
            {"Unit Number": "2BDR", "Unit type": "Villa", "Bedrooms": 2,
             "Price from (USD)": 130000, "Availability": "On sale",
             "Pool": "Yes(Private)"},
            {"Unit Number": "3BDR", "Unit type": "Villa", "Bedrooms": 3,
             "Availability": "On sale", "Pool": "Yes(Private)"},
        ],
        "_gaps": [
            "поюнитной шахматки Sunset Village нет, типология взята из папок рендеров (2 BDR / 3 BDR)",
            "цена 3BR неизвестна - в портале только «виллы 2BR от $130k»",
            "район юнитов не заполнен: в селекте Units.Area нет значения 'Kerobokan'",
        ],
    },
]


def fetch_kedungu_typology():
    """
    Шахматка Baza Kedungu, свёрнутая в ТИПОЛОГИЮ (решение владельца 02.08.2026).

    В таблице Units нужны типы, а не 58 отдельных юнитов: 27 одинаковых студий
    SMT — это одна строка каталога, а не 27. Группируем по буквенному префиксу
    номера (AL101 -> AL): именно так застройщик и раскладывает рендеры
    (папки AG, AL, ALT, AM, AXXLT, HMT, P, SL-SMT).

    Префикс обязателен как 'Unit Number': без него ключ upsert_unit вырождается
    в project__type__Nbr, и SL с SMT (обе Studio 1BR), как и AL/ALT/AM/AXXLT
    (все Apartment 1BR), схлопнулись бы в одну запись.
    """
    url = f"https://docs.google.com/spreadsheets/d/{KEDUNGU_MATRIX_SHEET}/export?format=csv"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8", errors="replace")

    groups, unknown = defaultdict(list), set()
    total = 0
    for row in csv.reader(io.StringIO(raw)):
        cells = [c.strip() for c in row]
        if len(cells) < 7:
            continue
        number, utype, m2, bedrooms, price, _price_m2, status = cells[:7]
        if not number or not utype or utype.lower() == "type" or not status:
            continue

        mapped_type = UNIT_TYPE_MAP.get(utype.lower())
        mapped_status = UNIT_STATUS_MAP.get(status.lower())
        prefix_match = re.match(r"^([A-Za-z]+)", number)
        if not mapped_type or not mapped_status or not prefix_match:
            unknown.add(f"{number} / {utype} / {status}")
            continue

        digits = re.sub(r"[^\d.]", "", price or "")
        total += 1
        groups[prefix_match.group(1).upper()].append({
            "type": mapped_type,
            "m2": float(m2) if re.match(r"^\d+(\.\d+)?$", m2 or "") else None,
            "beds": int(bedrooms) if (bedrooms or "").isdigit() else None,
            "price": float(digits) if digits else None,
            "status": mapped_status,
        })

    units = []
    for prefix in sorted(groups):
        items = groups[prefix]
        areas = [i["m2"] for i in items if i["m2"]]
        beds = [i["beds"] for i in items if i["beds"]]
        on_sale = [i for i in items if i["status"] == "On sale"]

        # Цена типологии — минимум среди юнитов В ПРОДАЖЕ. Проданные стоят по
        # старым, уже недоступным ценам ($79 000 против актуальных $92 500),
        # и такая цена в каталоге вводила бы брокера в заблуждение. Если
        # свободных нет вовсе, берём минимум по типологии и помечаем статусом.
        sale_prices = [i["price"] for i in on_sale if i["price"]]
        all_prices = [i["price"] for i in items if i["price"]]
        price = min(sale_prices) if sale_prices else (min(all_prices) if all_prices else None)

        if on_sale:
            availability = "On sale"
        elif any(i["status"] == "Blocked" for i in items):
            availability = "Blocked"
        else:
            availability = "Sold"

        units.append({
            "Unit Number": prefix,
            "Unit type": items[0]["type"],
            "Area from (m2)": min(areas) if areas else None,
            "Bedrooms": min(beds) if beds else None,
            "Price from (USD)": price,
            "Availability": availability,
            "Area": "Kedungu",
            "_count": len(items),
            "_on_sale": len(on_sale),
        })
    return units, unknown, total


def fetch_heights_typology():
    """
    'Full unit registry' The Heights (105 юнитов), свёрнутый в базовые типы.

    Побочные варианты стороны/этажа (Type 6-L/Type 6-R, Type 8.2) сведены к
    базовому Type N - это планировочные варианты одного типа юнита, не
    отдельные позиции каталога (тот же принцип, что и с префиксами Kedungu).
    Цена - "от", как и по Kedungu: area * HEIGHTS_PRICE_PER_M2 для площади
    самого дешёвого юнита типа. Точное площадь/цена по каждому юниту доступны
    в самой таблице, здесь агрегируются только для карточки каталога.
    """
    url = (f"https://docs.google.com/spreadsheets/d/{HEIGHTS_MATRIX_SHEET}"
           f"/gviz/tq?tqx=out:csv&gid={HEIGHTS_REGISTRY_GID}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=45) as resp:
        raw = resp.read().decode("utf-8", errors="replace")

    rows = [r for r in csv.reader(io.StringIO(raw)) if any(c.strip() for c in r)]
    groups, unknown = defaultdict(list), set()
    for row in rows[1:]:
        cells = [c.strip() for c in row]
        if len(cells) < 6:
            continue
        _unit_no, _floor, area, utype, _side, status = cells[:6]
        base_match = re.match(r"^(Type \d+)", utype)
        if not base_match or base_match.group(1) not in HEIGHTS_TYPE_MAP:
            unknown.add(utype)
            continue
        area_f = float(area.replace(",", ".")) if re.match(r"^[\d.,]+$", area) else None
        groups[base_match.group(1)].append({"area": area_f, "status": status.strip().lower()})

    units = []
    for base_type in sorted(groups, key=lambda t: int(re.search(r"\d+", t).group())):
        items = groups[base_type]
        unit_type, bedrooms = HEIGHTS_TYPE_MAP[base_type]
        areas = [i["area"] for i in items if i["area"]]
        available = [i for i in items if i["status"] == "available"]

        price = None
        if available:
            avail_areas = [i["area"] for i in available if i["area"]]
            if avail_areas:
                price = round(min(avail_areas) * HEIGHTS_PRICE_PER_M2)
        elif areas:
            price = round(min(areas) * HEIGHTS_PRICE_PER_M2)

        unit = {
            "Unit Number": base_type.replace(" ", "").upper(),
            "Unit type": unit_type,
            "Availability": "On sale" if available else "Sold",
            "_count": len(items),
            "_on_sale": len(available),
        }
        if bedrooms:
            unit["Bedrooms"] = bedrooms
        if areas:
            unit["Area from (m2)"] = min(areas)
        if price:
            unit["Price from (USD)"] = price
        units.append(unit)

    return units, unknown, sum(len(v) for v in groups.values())


def merge_developer(apply_changes: bool):
    """
    Сводит две записи застройщика в одну 'Bali Baza'.

    В базе лежат 'Baza' (на ней реальный проект Kedungu и контакты) и
    'Bali Baza' (на ней 'Blank…' и 'Every Day'). Оставляем ту, у которой
    настоящие данные, и переименовываем - так не рвутся существующие связи
    проекта Kedungu.
    """
    base = get_base()
    dev_table = base.table("Developer")
    proj_table = base.table("Projects")

    devs = dev_table.all()
    projects = proj_table.all()

    # Идемпотентность: после первого прогона запись уже называется 'Bali Baza',
    # и поиск строго по 'Baza' ничего не находил - повторный запуск падал на
    # ровном месте. Кандидаты: старое имя 'Baza' плюс все 'Bali Baza';
    # оставляем ту, у которой реально есть проекты (пустой дубль - расформируем).
    candidates = [r for r in devs
                  if (r["fields"].get("Developer") or "").strip() in ("Baza", "Bali Baza")]
    if not candidates:
        print("  ! ни 'Baza', ни 'Bali Baza' не найдены - слияние пропущено")
        return None, []

    candidates.sort(key=lambda r: len(r["fields"].get("Projects") or []), reverse=True)
    keep = candidates[0]
    dup = next((r for r in candidates[1:]
                if (r["fields"].get("Projects") or [])), None) or (
        candidates[1] if len(candidates) > 1 else None)

    moved = []
    if dup:
        for pid in (dup["fields"].get("Projects") or []):
            p = next((x for x in projects if x["id"] == pid), None)
            moved.append((pid, (p["fields"].get("Project Name") if p else "???")))

    print(f"  оставляем: '{keep['fields'].get('Developer')}' ({keep['id']}) -> переименуем в '{DEVELOPER_NAME}'")
    print(f"  контакты:  {keep['fields'].get('Contacts')!r} -> {DEVELOPER_CONTACTS!r}")
    if dup:
        print(f"  дубль:     '{dup['fields'].get('Developer')}' ({dup['id']}), проектов на нём: {len(moved)}")
        for pid, pname in moved:
            print(f"      переносим проект '{pname}' ({pid})")

    if apply_changes:
        robust_airtable_op(dev_table.update, keep["id"],
                           fields={"Developer": DEVELOPER_NAME, "Contacts": DEVELOPER_CONTACTS})
        for pid, _pname in moved:
            robust_airtable_op(proj_table.update, pid, fields={"Developer": [keep["id"]]})

    return keep["id"], moved


def find_existing(projects, *names):
    """
    Точное совпадение по любому из имён. Нечёткому сопоставлению здесь
    доверять нельзя ('Baza Kedungu' даёт fuzzy score 0.000 против 'Kedungu').

    Несколько имён нужны для идемпотентности: после первого прогона проект
    уже называется по-новому ('Baza Kedungu'), а старое имя ('Kedungu'), под
    которым его найти в первый раз, больше не существует. Поиск строго по
    старому имени на повторном запуске создал бы дубль.
    """
    targets = {n.strip().lower() for n in names if n}
    return next((r for r in projects
                 if (r["fields"].get("Project Name") or "").strip().lower() in targets), None)


def report_stale_units(existing_rec, all_units, new_keys):
    """
    Юниты, которые уже висят на проекте и НЕ будут перезаписаны новой типологией.

    На живых данных это чужие записи: у The Heights лежит апартамент за $89 000
    (цена Baza Kedungu) и вилла за $537 000 (цена Origins) - следы разбора
    сообщения, где упоминались сразу несколько проектов. Молча оставлять их
    рядом с настоящей типологией нельзя, но и удалять записи в этом проекте
    не принято - только показываем.
    """
    if not existing_rec:
        return []
    stale = []
    for uid in (existing_rec["fields"].get("Units") or []):
        u = next((x for x in all_units if x["id"] == uid), None)
        if not u:
            continue
        key = u["fields"].get("Key")
        if key not in new_keys:
            stale.append(u)
    return stale


async def run(apply_changes: bool, do_mirror: bool):
    mode = "ПРИМЕНЯЮ" if apply_changes else "ПЛАН (ничего не пишется)"
    print(f"=== ЛИСТИНГ ПРОЕКТОВ BALI BAZA — {mode} ===\n")

    print("1. Застройщик")
    dev_id, moved = merge_developer(apply_changes)
    if not dev_id:
        return

    kedungu_typology, kedungu_unknown, kedungu_total = fetch_kedungu_typology()
    print(f"\n2a. Шахматка Baza Kedungu: {kedungu_total} юнитов -> {len(kedungu_typology)} типологий")
    if kedungu_unknown:
        print(f"   ! не распознано: {kedungu_unknown}")
    for u in kedungu_typology:
        price = f"${u['Price from (USD)']:,.0f}" if u["Price from (USD)"] else "—"
        print(f"   {u['Unit Number']:10} {u['Unit type']:10} {u['Area from (m2)'] or '—':>6} м2  "
              f"{price:>10}  {u['Availability']:8} ({u['_count']} шт, свободно {u['_on_sale']})")

    heights_typology, heights_unknown, heights_total = fetch_heights_typology()
    print(f"\n2b. Реестр The Heights: {heights_total} юнитов -> {len(heights_typology)} типологий")
    if heights_unknown:
        print(f"   ! не распознано: {heights_unknown}")
    for u in heights_typology:
        price = f"${u['Price from (USD)']:,.0f}" if u.get("Price from (USD)") else "—"
        area = f"{u['Area from (m2)']:.1f}" if u.get("Area from (m2)") else "—"
        print(f"   {u['Unit Number']:8} {u['Unit type']:10} {area:>6} м2  {price:>10}  "
              f"{u['Availability']:8} ({u['_count']} шт, свободно {u['_on_sale']})")

    MATRICES = {
        "kedungu": kedungu_typology,
        "heights": heights_typology,
    }

    print("\n3. Проекты")
    base = get_base()
    proj_table = base.table("Projects")
    all_projects = proj_table.all()
    all_units = base.table("Units").all()
    if apply_changes:
        await init_cache_async(force=True)

    for spec in PROJECTS:
        proj = {k: v for k, v in spec.items() if not k.startswith("_")}
        name = proj["Project Name"]
        gaps = list(spec.get("_gaps") or [])
        existing = find_existing(all_projects, name, spec.get("_existing_name"))

        matrix_key = spec.get("_units_from_matrix")
        if matrix_key:
            source_typology = MATRICES[matrix_key]
            units = [{k: v for k, v in u.items() if not k.startswith("_")} for u in source_typology]
            # Цена проекта - минимум среди типологий В ПРОДАЖЕ: проданные стоят
            # по старым, уже недоступным ценам, и такая цена в карточке вводила
            # бы брокера в заблуждение.
            sale_prices = [u["Price from (USD)"] for u in source_typology
                           if u["Availability"] == "On sale" and u.get("Price from (USD)")]
            all_prices = [u["Price from (USD)"] for u in source_typology if u.get("Price from (USD)")]
            if sale_prices:
                proj["Price From (USD)"] = min(sale_prices)
            if all_prices:
                proj["Price To (USD)"] = max(all_prices)
        else:
            units = list(spec.get("_units") or [])

        where = (f"обновляем '{existing['fields'].get('Project Name')}' ({existing['id']})"
                 if existing else "СОЗДАЁМ новый")
        print(f"\n   [{name}] {where}")
        print(f"       район={proj.get('District')} цена от=${proj.get('Price From (USD)', '—')} "
              f"типологий к записи={len(units)}")
        for u in units:
            print(f"       {u.get('Unit Number'):10} {u.get('Unit type'):10} "
                  f"{u.get('Bedrooms') or '—'}BR")
        for g in gaps:
            print(f"       gap: {g}")

        proj_slug = re.sub(r'[^a-z0-9-]', '', name.lower().replace(' ', '-'))[:15]
        new_keys = {
            f"{proj_slug}__{str(u['Unit Number']).lower()}__{u.get('Bedrooms', 0)}br"
            for u in units
        }
        stale = report_stale_units(existing, all_units, new_keys)
        for u in stale:
            uf = u["fields"]
            print(f"       ! ЧУЖОЙ юнит -> отвязать + Draft: Key={uf.get('Key')} "
                  f"{uf.get('Unit type')} {uf.get('Price from(USD)')} USD")

        if not apply_changes:
            continue

        # Пишем по найденному id напрямую, а не через нечёткое сопоставление
        # upsert_project: 'Baza Kedungu' не матчится с существующим 'Kedungu'
        # (score 0.000) и создался бы дубль.
        if existing:
            fields = dict(proj)
            fields["Developer"] = [dev_id]
            fields["Gaps"] = ", ".join(gaps) if gaps else ""
            fields["Status"] = "Needs data" if gaps else "Verified"
            res = robust_airtable_op(proj_table.update, existing["id"], fields=fields)
            if not res.get("id"):
                # robust_airtable_op гасит HTTPError и возвращает {'id': None} -
                # обновление карточки провалилось ЦЕЛИКОМ (422 на одном поле
                # роняет весь запрос), а юниты ниже пишутся отдельным вызовом
                # и всё равно проедут. Молчать об этом нельзя - иначе кажется,
                # что карточка обновлена, а на деле старые поля остались.
                print(f"       !! ОБНОВЛЕНИЕ КАРТОЧКИ ПРОВАЛИЛОСЬ - поля проекта НЕ записаны "
                      f"(см. лог ошибки Airtable выше), юниты ниже всё равно запишутся")
            proj_id = res.get("id") or existing["id"]
        else:
            proj_id = await upsert_project(proj, dev_id, gaps)
        print(f"       -> проект {proj_id}")

        if proj_id and units:
            created = 0
            for u in units:
                if await upsert_unit(u, proj_id, name, []):
                    created += 1
            print(f"       -> типологий записано: {created} из {len(units)}")

        # Чужие юниты отвязываем от проекта и помечаем Draft (решение владельца
        # 02.08.2026). Не удаляем: в этом проекте записи не удаляются, а
        # архивируются - но и оставлять их в карточке нельзя, они показывают
        # цену другого проекта.
        if stale:
            unit_table = base.table("Units")
            detached = 0
            for u in stale:
                res = robust_airtable_op(unit_table.update, u["id"], fields={
                    "Project Name": [],
                    "Status": "Draft",
                    "Gaps": ("Отвязан 02.08.2026: запись не соответствует типологии "
                             f"застройщика по проекту {name} (следы разбора чужого проекта)"),
                })
                if res.get("id"):
                    detached += 1
            print(f"       -> чужих юнитов отвязано: {detached} из {len(stale)}")

    if do_mirror and apply_changes:
        print("\n4. Зеркалирование рендеров на Google Drive")
        from app.drive_mirror import mirror_drive_folder
        for project_name, folder_id in RENDER_FOLDERS.items():
            print(f"   {project_name} ...", flush=True)
            try:
                summary = await asyncio.to_thread(mirror_drive_folder, folder_id, project_name)
            except Exception as e:
                print(f"      ОШИБКА: {str(e)[:200]}")
                continue
            results = summary.get("results", [])
            copied = sum(1 for r in results if r["status"] == "copied")
            skipped = sum(1 for r in results if r["status"] == "skipped")
            existing = sum(1 for r in results if r["status"] == "exists")
            errors = [r for r in results if r["status"] == "error"]
            print(f"      скопировано {copied}, уже было {existing}, пропущено {skipped}, ошибок {len(errors)}")
            for r in errors[:5]:
                print(f"        ! {r['name']}: {r['reason']}")
    elif do_mirror:
        print("\n4. Зеркалирование: пропущено (нужен --apply)")

    if not apply_changes:
        print("\n[DRY RUN] Ничего не записано. Для применения:")
        print("   python tools_list_baza_projects.py --apply")
        print("   python tools_list_baza_projects.py --apply --mirror   (с рендерами)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Листинг проектов Bali Baza")
    parser.add_argument("--apply", action="store_true", help="Записать в Airtable")
    parser.add_argument("--mirror", action="store_true", help="Зеркалировать рендеры на Drive")
    args = parser.parse_args()
    asyncio.run(run(args.apply, args.mirror))
