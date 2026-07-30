import pytest
from app.priority_parser import (
    parse_priority,
    validate_priority,
    build_update_fields,
    enforce_legal_risk,
    to_airtable_priority,
)

class TestPriorityExtraction:
    """Тесты извлечения приоритета (Priority) из текста/комментариев агента."""

    @pytest.mark.parametrize("text", [
        "Срочно внести объект в базу!",
        "Это очень горячий объект в Убуде",
        "Горячая цена, нужно быстрее опубликовать",
        "Объект с вывески, СРОЧНО!",
        "Высокий приоритет для показа покупателю",
        "Топовый застройщик на Бали",
        "Супер локация в Семиньяке",
        "Перспективный район для покупки",
        "агент отмечает топовый проект",
        "горячий объект",
        "срочно",
        "Очень красивый проект виллы",
    ])
    def test_priority_high_keywords(self, text):
        assert parse_priority(text) == "Высокий"

    @pytest.mark.parametrize("text", [
        "Это пока просто черновик",
        "Информация не срочно, добавить по возможности",
        "Указать низкий приоритет",
        "ЧЕРНОВИК для проверки",
        "Ужасный подъезд к участку",
        "Сомнительный документ на землю",
        "Мутный продавец, не отвечает",
        "Мне совсем не нравится этот объект",
        "ужасный проект",
        "косячная стройка",
        "мутный застройщик",
    ])
    def test_priority_low_keywords(self, text):
        assert parse_priority(text) == "Низкий"

    @pytest.mark.parametrize("text", [
        "зеленая земля в Чангу",
        "строят на зеленой зоне",
        "нет документов на объект",
        "проблемы с документами у застройщика",
        "нет разрешения на строительство",
    ])
    def test_priority_low_legal_risks(self, text):
        assert parse_priority(text) == "Низкий"

    def test_priority_land_zoning_green_risk(self):
        # Если зонинг зеленый, приоритет должен автоматически становиться Низкий
        assert parse_priority("Обычный проект", land_zoning="Green") == "Низкий"
        assert parse_priority(None, land_zoning="Зеленая") == "Низкий"
        assert parse_priority("Топовый застройщик", handover_permits="No permit") == "Низкий"

    @pytest.mark.parametrize("text", [
        "роскошная вилла",
        "дизайнерский проект",
        "5 звезд",
        "Восхитительный вид на океан",
        "Вилла 2BR в Чангу за 250000 USD",
        "Обычный проект от застройщика",
        "Проект с 2 спальнями",
        "Стандартные условия рассрочки",
        "Апартаменты 50 кв.м.",
        "баннер выглядит выгоревшим",
        "баннер полузаброшен",
        "забор заслоняет контакты",
        "",
        "   ",
        None,
    ])
    def test_priority_medium_default(self, text):
        assert parse_priority(text) == "Средний"


class TestPriorityValidation:
    """Тесты валидации значения приоритета."""

    @pytest.mark.parametrize("valid_val", ["Высокий", "Средний", "Низкий", "Hight", "High", "Medium", "Low"])
    def test_validate_priority_valid_values(self, valid_val):
        assert validate_priority(valid_val) == valid_val

    @pytest.mark.parametrize("invalid_val", [
        "Very High",
        "Ultra Low",
        "Срочный",
        "Критический",
        "",
        None,
        123,
        ["Высокий"],
    ])
    def test_validate_priority_invalid_values(self, invalid_val):
        assert validate_priority(invalid_val) == "Средний"


class TestBuildUpdateFields:
    """Тесты формирования словаря update_fields для Airtable."""

    def test_build_update_fields_all_params(self):
        result = build_update_fields(
            priority="Высокий",
            dev_id="recDev123",
            final_proj_id="recProj456",
            contacts="+62812345678",
            final_notes="Комментарий агента",
            coords="-8.65, 115.13",
            status="Processed"
        )
        
        assert result == {
            "Status": "Processed",
            "Developer": ["recDev123"],
            "Project": ["recProj456"],
            "Contact": "https://wa.me/62812345678",
            "Notes": "Комментарий агента",
            # Airtable принимает только Hight/Medium/Low — русские значения отвергаются
            "Priority": "Hight",
            "Google Maps Link": "https://www.google.com/maps?q=-8.65,115.13"
        }

    def test_build_update_fields_removes_none_values(self):
        result = build_update_fields(
            priority="Низкий",
            dev_id=None,
            final_proj_id=None,
            contacts=None,
            final_notes=None,
            coords=None
        )

        assert result == {
            "Status": "Processed",
            "Priority": "Low"
        }
        assert "Developer" not in result
        assert "Project" not in result
        assert "Contact" not in result
        assert "Notes" not in result
        assert "Google Maps Link" not in result

    def test_build_update_fields_invalid_priority_fallback(self):
        result = build_update_fields(priority="UNKNOWN_PRIORITY")
        assert result["Priority"] == "Medium"


class TestLegalRiskOverride:
    """
    Приоритет из поля приходит уже готовым из ответа Gemini (по голосу агента).
    Юридический риск из документов застройщика должен понижать его независимо
    от того, проговорил агент этот риск вслух или нет.
    """

    def test_green_zoning_overrides_high_priority(self):
        assert enforce_legal_risk("Высокий", land_zoning="Green") == "Низкий"

    def test_missing_permit_overrides_high_priority(self):
        assert enforce_legal_risk("Высокий", handover_permits="No permit") == "Низкий"

    def test_safe_zoning_keeps_priority(self):
        assert enforce_legal_risk("Высокий", land_zoning="Residential") == "Высокий"

    def test_no_risk_data_keeps_priority(self):
        assert enforce_legal_risk("Высокий") == "Высокий"

    def test_build_update_fields_applies_override(self):
        """Ключевая проверка: понижение работает в реальной точке записи в Airtable."""
        result = build_update_fields(
            priority="Высокий",
            land_zoning="Green",
        )
        assert result["Priority"] == "Low"

    def test_build_update_fields_without_risk_is_unchanged(self):
        result = build_update_fields(
            priority="Высокий",
            land_zoning="Tourism/Mixed",
            handover_permits="PBG",
        )
        assert result["Priority"] == "Hight"


class TestAirtablePriorityMapping:
    """
    Значения singleSelect 'Priority' в Field Staging: Hight / Medium / Low.
    Любое другое значение Airtable отвергает, запись падает и помечается Error.
    """

    @pytest.mark.parametrize("value,expected", [
        ("Высокий", "Hight"),
        ("High", "Hight"),
        ("Hight", "Hight"),
        ("Средний", "Medium"),
        ("Medium", "Medium"),
        ("Низкий", "Low"),
        ("Low", "Low"),
        ("  низкий  ", "Low"),
        ("ВЫСОКИЙ", "Hight"),
    ])
    def test_maps_to_existing_airtable_option(self, value, expected):
        assert to_airtable_priority(value) == expected

    @pytest.mark.parametrize("value", ["Критический", "", None, 123, ["Высокий"]])
    def test_unknown_values_fall_back_to_medium(self, value):
        assert to_airtable_priority(value) == "Medium"

    def test_every_output_is_a_real_airtable_option(self):
        allowed = {"Hight", "Medium", "Low"}
        for src in ["Высокий", "Средний", "Низкий", "High", "Medium", "Low", "мусор", None]:
            assert to_airtable_priority(src) in allowed
