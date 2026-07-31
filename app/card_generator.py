"""
Генератор коммерческих карточек проектов для клиентов.

1. format_telegram_project_post(): Текстовый стильный пост в Telegram с эмодзи.
2. generate_pdf_project_card(): PDF-документ карточки проекта (через reportlab).
"""

import os
import re
import tempfile
import logging
from typing import Dict, List, Optional, Any

logger = logging.getLogger("CardGenerator")


def extract_link_url(raw_link: Any) -> Optional[str]:
    """Извлекает строковый URL из строки, списка или словаря Airtable."""
    if not raw_link:
        return None
    if isinstance(raw_link, str) and raw_link.startswith('http'):
        return raw_link
    if isinstance(raw_link, (list, tuple)) and len(raw_link) > 0:
        return extract_link_url(raw_link[0])
    if isinstance(raw_link, dict) and 'url' in raw_link:
        return raw_link['url']
    return None


def format_telegram_project_post(proj_data: Dict[str, Any],
                                dev_data: Optional[Dict[str, Any]] = None,
                                units: Optional[List[Dict[str, Any]]] = None) -> str:
    """
    Форматирует данные проекта в красивый готовый пост для Telegram с эмодзи.
    """
    proj_name = proj_data.get('Project Name') or proj_data.get('name') or 'Проект недвижимости'
    district = proj_data.get('District') or proj_data.get('Location') or 'Бали'

    # Разрезолвить ID застройщика в имя через кэш
    raw_dev = (dev_data.get('Developer') if dev_data else None) or proj_data.get('Developer')
    if isinstance(raw_dev, (list, tuple)) and raw_dev:
        raw_dev = raw_dev[0]
    dev_name = 'Застройщик'
    if raw_dev and not str(raw_dev).startswith('rec'):
        dev_name = str(raw_dev)
    elif raw_dev and str(raw_dev).startswith('rec'):
        try:
            from app.airtable_client import CACHE_DEVELOPERS, init_cache
            if not CACHE_DEVELOPERS:
                init_cache()
            for d in CACHE_DEVELOPERS:
                if d.get('id') == raw_dev:
                    dev_name = d.get('fields', {}).get('Developer') or raw_dev
                    break
        except Exception:
            pass
    stage = proj_data.get('Construction stage') or 'В процессе строительства'
    
    price_from = proj_data.get('Price From (USD)') or proj_data.get('Price from(USD)')
    price_to = proj_data.get('Price To (USD)')

    # Если цены нет на уровне проекта — берём мин/макс из юнитов
    if not price_from and units:
        unit_prices = [u.get('Price from(USD)') or u.get('Price From (USD)') for u in units]
        unit_prices = [p for p in unit_prices if p]
        if unit_prices:
            price_from = min(unit_prices)
            price_to = max(unit_prices) if len(unit_prices) > 1 else None

    if price_from and price_to and price_to != price_from:
        price_str = f"${price_from:,.0f} — ${price_to:,.0f}"
    elif price_from:
        price_str = f"от ${price_from:,.0f}"
    else:
        price_str = "по запросу"

    lease_term = proj_data.get('Lease Term (years)')
    lease_str = f"{lease_term} лет" if lease_term else "Freehold / Leasehold"
    
    beach_dist = proj_data.get('Distance to the beach, m2') or proj_data.get('Distance to beach')
    beach_str = f"🌊 До пляжа: {beach_dist} м\n" if beach_dist else ""

    renders_link = extract_link_url(proj_data.get('Renders') or proj_data.get('Project Cloud') or proj_data.get('Img'))
    chart_link = extract_link_url(proj_data.get('Availability Chart') or proj_data.get('Agent Portal Link'))
    maps_link = extract_link_url(proj_data.get('Google Maps Link') or proj_data.get('Location Link'))

    lines = [
        f"🏛 *{proj_name.upper()}*",
        f"📍 *Локация:* {district}",
        f"🏢 *Застройщик:* {dev_name}",
        f"🏗 *Стадия:* {stage}",
        f"⏳ *Форма владения:* {lease_str}",
        f"💰 *Стоимость:* {price_str}",
        beach_str.strip(),
    ]
    lines = [l for l in lines if l]

    if units:
        lines.append("\n🏠 *Варианты юнитов:*")
        for u in units[:6]:
            u_type = u.get('Unit type') or 'Юнит'
            beds = u.get('Bedrooms')
            beds_str = f"{beds}BR" if beds else ""
            u_price = u.get('Price from(USD)') or u.get('Price From (USD)')
            u_price_str = f"от ${u_price:,.0f}" if u_price else "цена по запросу"
            lines.append(f" • {u_type} {beds_str} — {u_price_str}")

    links_section = []
    if renders_link:
        links_section.append(f"🖼 [Рендеры и презентация]({renders_link})")
    if chart_link:
        links_section.append(f"📊 [Шахматка и наличие]({chart_link})")
    if maps_link:
        links_section.append(f"🗺 [Локация на карте]({maps_link})")

    if links_section:
        lines.append("\n🔗 *Материалы и ссылки:*")
        lines.extend(links_section)

    lines.append("\n📞 _Для получения брошюры и условий свяжитесь с менеджером._")

    return "\n".join(lines)


def generate_pdf_project_card(proj_data: Dict[str, Any],
                             dev_data: Optional[Dict[str, Any]] = None,
                             units: Optional[List[Dict[str, Any]]] = None,
                             output_path: Optional[str] = None) -> str:
    """
    Генерирует PDF-документ карточки проекта через ReportLab.
    """
    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

    if not output_path:
        temp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        output_path = temp.name
        temp.close()

    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        rightMargin=40, leftMargin=40, topMargin=40, bottomMargin=40
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Heading1'],
        fontSize=22,
        leading=26,
        textColor=colors.HexColor('#1E293B'),
        spaceAfter=10
    )
    subtitle_style = ParagraphStyle(
        'SubTitle',
        parent=styles['Normal'],
        fontSize=12,
        leading=16,
        textColor=colors.HexColor('#64748B'),
        spaceAfter=20
    )
    heading2_style = ParagraphStyle(
        'Heading2',
        parent=styles['Heading2'],
        fontSize=14,
        leading=18,
        textColor=colors.HexColor('#0F172A'),
        spaceBefore=15,
        spaceAfter=8
    )
    body_style = ParagraphStyle(
        'Body',
        parent=styles['Normal'],
        fontSize=10,
        leading=14,
        textColor=colors.HexColor('#334155')
    )

    story = []

    proj_name = proj_data.get('Project Name') or proj_data.get('name') or 'Проект недвижимости'
    district = proj_data.get('District') or proj_data.get('Location') or 'Бали'
    dev_name = (dev_data.get('Developer') if dev_data else None) or proj_data.get('Developer') or 'Застройщик'

    story.append(Paragraph(proj_name, title_style))
    story.append(Paragraph(f"Локация: {district} | Застройщик: {dev_name}", subtitle_style))

    # Сводная таблица параметров
    price_from = proj_data.get('Price From (USD)') or proj_data.get('Price from(USD)')
    price_to = proj_data.get('Price To (USD)')
    if price_from and price_to:
        price_str = f"${price_from:,.0f} - ${price_to:,.0f}"
    elif price_from:
        price_str = f"от ${price_from:,.0f}"
    else:
        price_str = "По запросу"

    table_data = [
        [Paragraph("<b>Параметр</b>", body_style), Paragraph("<b>Значение</b>", body_style)],
        [Paragraph("Стадия строительства", body_style), Paragraph(str(proj_data.get('Construction stage', 'В процессе')), body_style)],
        [Paragraph("Форма владения", body_style), Paragraph(f"{proj_data.get('Lease Term (years)', 'Leasehold')} лет", body_style)],
        [Paragraph("Стоимость объектов", body_style), Paragraph(price_str, body_style)],
        [Paragraph("Расстояние до пляжа", body_style), Paragraph(f"{proj_data.get('Distance to the beach, m2', 'Н/Д')} м", body_style)],
    ]

    t = Table(table_data, colWidths=[200, 300])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F1F5F9')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor('#0F172A')),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
    ]))
    story.append(t)

    if units:
        story.append(Paragraph("Типы юнитов и планировки", heading2_style))
        unit_rows = [[Paragraph("<b>Тип</b>", body_style), Paragraph("<b>Спальни</b>", body_style), Paragraph("<b>Цена (USD)</b>", body_style)]]
        for u in units:
            u_type = str(u.get('Unit type', 'Юнит'))
            beds = str(u.get('Bedrooms', '-'))
            u_price = u.get('Price from(USD)') or u.get('Price From (USD)')
            u_price_str = f"${u_price:,.0f}" if u_price else "По запросу"
            unit_rows.append([Paragraph(u_type, body_style), Paragraph(beds, body_style), Paragraph(u_price_str, body_style)])

        ut = Table(unit_rows, colWidths=[180, 120, 200])
        ut.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F8FAFC')),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
        ]))
        story.append(ut)

    story.append(Spacer(1, 25))
    story.append(Paragraph("Rise Real Estate Bali — База объектов недвижимости", body_style))

    doc.build(story)
    logger.info(f"PDF карточка сформирована: {output_path}")
    return output_path
