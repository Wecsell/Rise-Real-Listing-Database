import pytest
from app.phone_formatter import format_contacts_international, format_single_phone, format_whatsapp_link


class TestFormatSinglePhone:
    """Тесты для функции format_single_phone (форматирование одиночного номера)."""

    def test_local_indonesian_number_with_zero(self):
        """1) Локальный индонезийский номер с ведущим 0."""
        assert format_single_phone("08133888995") == "https://wa.me/628133888995"

    def test_international_indonesian_number_with_plus_and_dashes(self):
        """2) Международный индонезийский номер с плюсом, пробелами и дефисами."""
        assert format_single_phone("+62 813-3919-882") == "https://wa.me/628133919882"

    def test_australian_number_with_spaces(self):
        """3) Международный австралийский номер с пробелами."""
        assert format_single_phone("+61 434 639 068") == "https://wa.me/61434639068"

    @pytest.mark.parametrize("invalid_input, expected", [
        ("", ""),
        ("   ", ""),
        (None, ""),
        ("123456", "123456"),  # меньше 7 цифр
        ("abc", "abc"),
    ])
    def test_empty_or_invalid_inputs(self, invalid_input, expected):
        """5) Пустые или некорректные значения."""
        assert format_single_phone(invalid_input) == expected

    def test_already_wa_me_link(self):
        """Проверка уже существующей wa.me ссылки."""
        assert format_single_phone("https://wa.me/628133888995") == "https://wa.me/628133888995"
        assert format_single_phone("wa.me/08133888995") == "https://wa.me/628133888995"


class TestFormatWhatsappLink:
    """Тесты для основной функции format_whatsapp_link (обработка строк контактов)."""

    def test_format_indonesian_zero_prefix(self):
        """1) '08133888995' -> 'https://wa.me/628133888995'."""
        assert format_whatsapp_link("08133888995") == "https://wa.me/628133888995"

    def test_format_indonesian_plus_formatted(self):
        """2) '+62 813-3919-882' -> 'https://wa.me/628133919882'."""
        assert format_whatsapp_link("+62 813-3919-882") == "https://wa.me/628133919882"

    def test_format_australian_formatted(self):
        """3) '+61 434 639 068' -> 'https://wa.me/61434639068'."""
        assert format_whatsapp_link("+61 434 639 068") == "https://wa.me/61434639068"

    def test_format_combination_phone_and_telegram_handle(self):
        """4) комбинации '081 805 629 289, @RemaxThrone' -> 'https://wa.me/6281805629289, @RemaxThrone'."""
        assert format_whatsapp_link("081 805 629 289, @RemaxThrone") == "https://wa.me/6281805629289, @RemaxThrone"

    def test_format_order_social_first_phone_second(self):
        """Проверка правильного порядка вывода: если соцсеть идет первой, WhatsApp ссылка должна стать первой."""
        assert format_whatsapp_link("@RemaxThrone, 081 805 629 289") == "https://wa.me/6281805629289, @RemaxThrone"

    @pytest.mark.parametrize("invalid_input, expected", [
        (None, None),
        ("", ""),
        ("   ", ""),
        ("@only_handle", "@only_handle"),
        ("https://example.com", "https://example.com"),
        ("No phone here", "No phone here"),
        (12345, 12345),
    ])
    def test_format_empty_or_invalid_inputs(self, invalid_input, expected):
        """5) пустые или некорректные значения."""
        assert format_whatsapp_link(invalid_input) == expected

    def test_format_multiple_contacts(self):
        """Проверка списка из нескольких контактов через запятую и перенос строки."""
        input_str = "08133888995, +61 434 639 068\n+62 813-3919-882, @agent_nick"
        expected = "https://wa.me/628133888995, https://wa.me/61434639068, https://wa.me/628133919882, @agent_nick"
        assert format_whatsapp_link(input_str) == expected


class TestFormatContactsInternational:
    """
    Единый вид "+код номер" для колонки Developer.Contacts (задача: 142 живых
    записи, часть уже '+62 ...', часть в локальном формате с 0, часть вообще
    не телефон). Кейсы взяты из реальных значений, увиденных живым чтением базы.
    """

    def test_already_formatted_phone_is_left_equivalent(self):
        assert format_contacts_international("+62 821 4424119") == "+62 8214424119"

    def test_local_indonesian_zero_prefix_becomes_plus_62(self):
        assert format_contacts_international("0858-4764-6202") == "+62 85847646202"

    def test_bare_local_number_with_spaces(self):
        assert format_contacts_international("081 558 615 999") == "+62 81558615999"

    def test_handle_only_is_untouched(self):
        assert format_contacts_international("@Agent_BaliBaza, @DanBaliBaza") == "@Agent_BaliBaza, @DanBaliBaza"

    def test_website_is_untouched(self):
        assert format_contacts_international("cemagirock.com") == "cemagirock.com"

    def test_qr_code_placeholder_is_untouched(self):
        assert format_contacts_international("QR code") == "QR code"

    def test_phone_with_trailing_annotation_keeps_annotation(self):
        """'+6281330820767 (Jeff whatsapp)' - номер переформатирован, пометка на месте."""
        result = format_contacts_international("+6281330820767 (Jeff whatsapp)")
        assert result == "+62 81330820767 (Jeff whatsapp)"

    def test_certificate_like_number_is_not_mistaken_for_a_phone(self):
        """
        '96.2.22.01.04.001' - номер сертификата/акта, а не телефон: цифры
        разбиты точками на короткие группы, ни одна не даёт непрерывный
        телефон-подобный кусок длиной от 8 цифр.
        """
        result = format_contacts_international("96.2.22.01.04.001, +62 823-5935-4250")
        assert result == "96.2.22.01.04.001, +62 82359354250"

    def test_website_and_phone_mixed_only_phone_reformatted(self):
        result = format_contacts_international("+62 822 3089 1640, www.luxurypalmbali.com")
        assert result == "+62 82230891640, www.luxurypalmbali.com"

    def test_wa_me_link_is_left_untouched(self):
        """Ссылки wa.me - отдельный, уже решённый формат; эта функция про '+код номер', не про них."""
        result = format_contacts_international("https://wa.me/6281234567890")
        assert result == "https://wa.me/6281234567890"

    @pytest.mark.parametrize("invalid_input, expected", [
        (None, None),
        ("", ""),
        ("   ", ""),
        (12345, 12345),
    ])
    def test_empty_or_invalid_inputs(self, invalid_input, expected):
        assert format_contacts_international(invalid_input) == expected
