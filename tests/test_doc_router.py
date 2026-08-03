import os
import sys
import unittest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.doc_router import (
    classify_document,
    fields_closed_by,
    is_document_scan,
    is_parsable,
    is_skipped_document,
    needs_vision,
    route_files_for_gaps,
    DEFAULT_OPEN_BUDGET,
)


def _f(name, mime="application/pdf", path=""):
    return {"id": name, "name": name, "mimeType": mime, "path": path}


class TestClassification(unittest.TestCase):
    """
    Имена взяты с реальной Legal-папки Four Palms - там документы застройщика
    названы на bahasa, и именно аббревиатуры (ITR, PBG, SMT) опознают файл,
    а не английские слова.
    """

    def test_real_four_palms_names(self):
        cases = {
            "SMT Four Palms - Kedungu.pdf": "lease",
            "KKPR Usaha Micro (SPUMPTR) - TATA RUANG SHM 00577 Bengkel.pdf": "zoning",
            "PBG Four Palms.pdf": "permits",
            "PT Seacrest RE - NIB.pdf": "company",
            "Four Palms Kedungu - ENG Pricing June'2026.pdf": "pricing",
        }
        for filename, expected in cases.items():
            self.assertEqual(classify_document(filename), expected, filename)

    def test_akta_pendirian_is_company_not_lease(self):
        """
        Регрессия на реальный файл Four Palms: общий \\bakta\\b (lease) перебивал
        специфичный "akta pendirian" (company), и учредительный акт компании
        уезжал в срок аренды.
        """
        self.assertEqual(
            classify_document("PT Seratus MPI - AKTA PENDIRIAN-6.pdf"), "company"
        )

    def test_pt_prefix_does_not_shadow_real_doc_type(self):
        """
        В папке застройщика почти каждый файл назван "PT <компания> - <что это>".
        Слабый признак "PT " не должен перебивать настоящий тип документа.
        """
        self.assertEqual(classify_document("PT Seacrest RE - SMT Kedungu.pdf"), "lease")
        self.assertEqual(classify_document("PT Seacrest RE - PBG.pdf"), "permits")

    def test_unknown_name_is_none_not_garbage(self):
        """Неопознанное - кандидат на дорогой fallback, а не мусор."""
        self.assertIsNone(classify_document("scan_2026_final_v3.pdf"))

    def test_folder_on_path_classifies_generic_filename(self):
        """У застройщиков осмысленное имя часто на папке, а не на файле."""
        self.assertEqual(
            classify_document("scan_01.pdf", path="03 Legal/Sewa Menyewa"), "lease"
        )

    def test_weak_marketing_words_do_not_outrank_real_document_type(self):
        """
        Регрессия 02.08.2026: шаблон presentation с общими словами (info, about,
        overview, concept, deck) стоял ВЫШЕ certificate и совпадал с путём, а не
        только с именем файла. "01 Legal/General Info/SHM Certificate.pdf"
        уезжал в presentation, и сертификат переставал закрывать Land Zoning Color.
        """
        self.assertEqual(
            classify_document("SHM Certificate.pdf", path="01 Legal/General Info"),
            "certificate",
        )
        self.assertEqual(
            classify_document("NPWP.pdf", path="02 Company Info"), "company"
        )
        self.assertEqual(
            classify_document("ITR.pdf", path="Legal/Info"), "zoning"
        )

    def test_fields_map_matches_gaps_field_names(self):
        """
        Роутер должен говорить именами полей из app/gaps.py - иначе сопоставление
        "пустое поле -> документ" пришлось бы дублировать вручную.
        """
        from app.gaps import REQUIRED_PROJECT_FIELDS
        for doc_type in ("zoning", "permits"):
            for field in fields_closed_by(doc_type):
                self.assertIn(field, REQUIRED_PROJECT_FIELDS, f"{doc_type} -> {field}")


class TestSkipList(unittest.TestCase):
    """План, Э1: паспорта и KTP не скачиваем, не парсим, не прикладываем."""

    def test_passport_is_skipped(self):
        self.assertTrue(is_skipped_document("PT Seacrest RE - KITAP Passport Vasily Pronin.jpg"))
        self.assertFalse(is_parsable(_f("Passport Direktur.pdf")))

    def test_ktp_is_skipped(self):
        self.assertFalse(is_parsable(_f("KTP Direktur.pdf")))

    def test_skip_list_catches_folder_on_path(self):
        self.assertFalse(is_parsable(_f("scan_01.pdf", path="Legal/Passports")))

    def test_ordinary_legal_doc_is_parsable(self):
        """Обратная сторона: настоящий документ не должен ложно отсекаться."""
        self.assertTrue(is_parsable(_f("Akta Pendirian PT Seacrest.pdf")))

    def test_renders_are_not_parsed(self):
        """Рендеры нужны ссылкой на лендинге, а не как источник полей (план)."""
        self.assertFalse(is_parsable(_f("3.1.jpg", mime="image/jpeg")))
        self.assertFalse(is_parsable(_f("tour.mov", mime="video/quicktime")))

    def test_document_scan_image_is_parsed_unlike_render(self):
        """
        Регрессия на реальную дыру: в Legal-папке Four Palms единственное
        свидетельство разрешений - "SLF reg.jpeg", скриншот из SIMBG. Правило
        "фото не парсим" написано про рендеры; отсекая все картинки, мы теряли
        разбор, который план прямо предусматривает (тест 12 плана).
        """
        scan = _f("SLF reg.jpeg", mime="image/jpeg")
        self.assertTrue(is_parsable(scan))
        self.assertTrue(is_document_scan(scan))
        self.assertFalse(is_document_scan(_f("3.1.jpg", mime="image/jpeg")))

    def test_render_in_marketing_folder_is_not_a_document_scan(self):
        """
        Регрессия 02.08.2026: presentation/location ловили путь рендера
        ("Renders/Masterplan/3.1.jpg", "Deck area/pool.jpg"), is_document_scan
        начинал возвращать True, и рендеры уезжали в дорогое зрение.
        """
        for name, path in (
            ("3.1.jpg", "Renders/Masterplan"),
            ("view_07.jpg", "Exterior/Concept"),
            ("bedroom.jpg", "Interior/Overview"),
            ("pool.jpg", "Deck area"),
            ("plan.jpg", "Site map"),
        ):
            info = _f(name, mime="image/jpeg", path=path)
            self.assertFalse(is_document_scan(info), f"{path}/{name}")
            self.assertFalse(is_parsable(info), f"{path}/{name}")

    def test_passport_scan_stays_skipped_even_though_it_is_a_document(self):
        """Skip-лист сильнее правила про сканы: паспорт - тоже 'документ'."""
        self.assertFalse(is_parsable(_f("KITAP Passport Pronin.jpg", mime="image/jpeg")))

    def test_image_is_flagged_for_vision(self):
        """План: текстовый слой раньше зрения; у картинки его нет по определению."""
        self.assertTrue(needs_vision(_f("SLF reg.jpeg", mime="image/jpeg")))
        self.assertFalse(needs_vision(_f("PBG permit.pdf")))


class TestRoutingUnderBudget(unittest.TestCase):

    def test_document_for_filled_field_is_not_opened(self):
        """
        Ядро экономии Э2: файл, закрывающий уже заполненное поле, не открывается.
        Здесь Land Zoning Color заполнен, значит ITR открывать незачем.
        """
        files = [_f("ITR Zoning.pdf"), _f("PBG permit.pdf")]
        res = route_files_for_gaps(files, empty_fields={"Handover Permits"})

        opened = [item["file"]["name"] for item in res["to_open"]]
        self.assertEqual(opened, ["PBG permit.pdf"])
        self.assertTrue(any("already-filled" in s["reason"] for s in res["skipped"]))

    def test_budget_is_respected_with_many_candidates(self):
        """План, тест 4: при 50 файлах открыто не больше N."""
        files = [_f(f"PBG permit {i}.pdf") for i in range(50)]
        res = route_files_for_gaps(files, empty_fields={"Handover Permits"})
        self.assertEqual(len(res["to_open"]), DEFAULT_OPEN_BUDGET)

    def test_more_fields_closed_means_higher_priority(self):
        files = [_f("PBG permit.pdf"), _f("SMT lease agreement.pdf")]
        res = route_files_for_gaps(
            files,
            empty_fields={"Handover Permits", "Lease Term (years)", "Ownership Type"},
            budget=1,
        )
        # lease закрывает два пустых поля против одного у permits
        self.assertEqual(res["to_open"][0]["file"]["name"], "SMT lease agreement.pdf")

    def test_budget_covers_different_fields_before_duplicates(self):
        """
        Регрессия на реальный случай Legal-папки Four Palms: там 4 zoning-документа
        и 1 permits. Простая сортировка отдавала все места бюджета zoning'у -
        четыре дорогих открытия ради ОДНОГО поля, а единственный PBG, закрывавший
        отдельное пустое поле, вытеснялся вовсе.
        """
        files = [
            _f("ITR one.pdf"), _f("ITR two.pdf"), _f("PKKPR three.pdf"),
            _f("TATA RUANG four.pdf"), _f("PBG permit.pdf"),
        ]
        res = route_files_for_gaps(
            files, empty_fields={"Land Zoning Color", "Handover Permits"}, budget=2
        )
        opened_types = {item["doc_type"] for item in res["to_open"]}
        self.assertEqual(opened_types, {"zoning", "permits"})

    def test_duplicates_still_taken_on_leftover_budget(self):
        """Дубль по уже покрытому полю - подстраховка, но не в ущерб охвату."""
        files = [_f("ITR one.pdf"), _f("ITR two.pdf")]
        res = route_files_for_gaps(files, empty_fields={"Land Zoning Color"}, budget=5)
        self.assertEqual(len(res["to_open"]), 2)

    def test_unknown_files_only_use_leftover_budget(self):
        """
        Fallback по решению владельца: неопознанные открываются, но только
        после опознанных и только на остатке бюджета.
        """
        files = [_f("scan_a.pdf"), _f("scan_b.pdf"), _f("PBG permit.pdf")]
        res = route_files_for_gaps(files, empty_fields={"Handover Permits"}, budget=2)

        opened = [item["file"]["name"] for item in res["to_open"]]
        self.assertEqual(opened[0], "PBG permit.pdf")
        self.assertEqual(len(opened), 2)

    def test_no_empty_fields_means_nothing_is_opened(self):
        """Карточка полная - открывать нечего, даже неопознанное."""
        files = [_f("PBG permit.pdf"), _f("scan_a.pdf")]
        res = route_files_for_gaps(files, empty_fields=set())
        self.assertEqual(res["to_open"], [])

    def test_passport_never_enters_budget_even_when_unknown(self):
        """
        Опасный случай: паспорт по имени не классифицируется ни в один тип,
        то есть попал бы в unknown и был бы открыт как fallback-кандидат.
        Skip-лист должен сработать раньше классификации.
        """
        files = [_f("Passport Vasily Pronin.pdf")]
        res = route_files_for_gaps(files, empty_fields={"Handover Permits"})
        self.assertEqual(res["to_open"], [])
        self.assertEqual(res["unknown"], [])
        self.assertEqual(res["skipped"][0]["reason"], "identity document")

    def test_combined_typology_file_preferred_over_near_duplicates(self):
        """
        Регрессия на реальный случай Mangata (03.08.2026): в папке 4 почти
        идентичных файла "...3BR.pdf" (RU/EN, с лого и без) и один
        "...2_3BR.pdf" в подпапке "Presentation FULL (1,2,3 BR)". Стадия
        готовности (83%) была написана только в сводном файле, но при
        budget=1 порядок листинга Drive API отдавал слот случайному дублю.

        _specificity_score должен предпочесть сводный файл: и по "FULL" в
        пути, и по числу разных упоминаний спален (2 и 3) против одного (3).
        """
        files = [
            _f("RUS_Mangata residence_3BR.pdf", path="7. Presentations/3BR townhouse"),
            _f("ENG_Mangata residence_3BR.pdf", path="7. Presentations/3BR townhouse"),
            _f("ENG_Mangata residence_3BR_NO LOGO.pdf",
               path="7. Presentations/3BR townhouse/NO LOGO 3 BR"),
            _f("RUS_Mangata residence_2_3BR.pdf",
               path="7. Presentations/Presentation FULL (1,2,3 BR)"),
        ]
        res = route_files_for_gaps(
            files, empty_fields={"Construction stage"}, budget=1
        )
        self.assertEqual(len(res["to_open"]), 1)
        self.assertEqual(res["to_open"][0]["file"]["name"], "RUS_Mangata residence_2_3BR.pdf")

    def test_specificity_tie_break_also_applies_to_duplicate_fill(self):
        """Тот же приоритет должен работать и на дублях, добираемых сверх бюджета."""
        files = [
            _f("ITR narrow.pdf", path="Legal/3BR"),
            _f("ITR full masterplan.pdf", path="Legal/Presentation FULL (1,2,3 BR)"),
        ]
        res = route_files_for_gaps(
            files, empty_fields={"Land Zoning Color"}, budget=1
        )
        self.assertEqual(res["to_open"][0]["file"]["name"], "ITR full masterplan.pdf")


if __name__ == '__main__':
    unittest.main()
