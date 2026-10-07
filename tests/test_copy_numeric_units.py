"""Numbers with incompatible or unknown units cannot borrow unrelated facts."""
import unittest

from pipeline.copy_evidence import _measurements, candidate_evidence


def fact(value, unit, identity="facts.package_quantity.value"):
    return {"id": identity, "value": value, "unit": unit, "verified": True,
            "kind": "specification", "allow_numeric": True}


def bundle(claim, identity=None):
    return {"title_ru": f"Игрушка антистресс, {claim}", "description_ru": "Игрушка для игры.",
            "primary_keywords": [], "secondary_keywords": [],
            "claim_evidence": [{"claim": claim, "fact_ids": [identity]}] if identity else []}


class CopyNumericUnitTests(unittest.TestCase):
    def test_package_quantity_cannot_prove_age_duration_or_battery_capacity(self):
        row = fact(2, "шт")
        for claim in ("от 2 лет", "от 2 года", "от 2 месяцев", "работает 2 часа", "служит 2 дня",
                      "работает 2 минуты", "работает 2 секунды", "емкость 2 мАч", "емкость 2 Ач",
                      "емкость 2 литра", "2 процента", "2%", "2 неизвестных"):
            for explicit in (False, True):
                with self.subTest(claim=claim, explicit=explicit), self.assertRaises(ValueError):
                    candidate_evidence(bundle(claim, row["id"] if explicit else None), [row], [])

    def test_unitless_fact_cannot_prove_known_or_unknown_unit(self):
        row = fact(2, "", "facts.skus.selected.properties.number")
        for claim in ("2 лет", "2 часа", "2 мАч", "2 литра", "2 фута", "2 г", "2%"):
            for explicit in (False, True):
                with self.subTest(claim=claim, explicit=explicit), self.assertRaises(ValueError):
                    candidate_evidence(bundle(claim, row["id"] if explicit else None), [row], [])

    def test_exact_supported_unit_aliases_and_quantity_remain_valid(self):
        for value, unit, claim in ((2, "шт", "2 штуки"), (2, "year", "от 2 лет"),
                                   (2, "month", "от 2 месяцев"), (2, "hour", "2 часа"),
                                   (2, "day", "2 дня"), (2, "minute", "2 минуты"),
                                   (2, "second", "2 секунды"), (2, "mah", "2 мАч"),
                                   (2, "ah", "2 Ач"), (2, "ml", "2 миллилитра"),
                                   (2, "l", "2 литра"), (2, "kg", "2 килограмма"),
                                   (2, "%", "2%"), ("2,50", "cm", "2.5 см")):
            row = fact(value, unit, "facts.skus.selected.properties.verified_measurement")
            for explicit in (False, True):
                with self.subTest(unit=unit, explicit=explicit):
                    result = candidate_evidence(bundle(claim, row["id"] if explicit else None), [row], [])
                    self.assertTrue(result["claim_evidence"])

    def test_unknown_unit_never_matches_a_different_unknown_word(self):
        row = fact("2 фута", "", "facts.skus.selected.properties.length")
        candidate_evidence(bundle("2 фута", row["id"]), [row], [])
        for claim in ("2 ярда", "2 галлона", "2"):
            with self.subTest(claim=claim), self.assertRaises(ValueError):
                candidate_evidence(bundle(claim), [row], [])

    def test_missing_claim_unit_is_not_a_wildcard(self):
        with self.assertRaises(ValueError):
            candidate_evidence(bundle("2"), [fact(2, "шт")], [])
        candidate_evidence(bundle("2"), [fact(2, "")], [])

    def test_exact_decimal_and_integer_normalization(self):
        self.assertEqual(_measurements("100.000", "г"), {("100", "g")})
        self.assertEqual(_measurements("0,00010", "мл"), {("0.0001", "ml")})
        self.assertEqual(_measurements("12345678901234567890", "мАч"),
                         {("12345678901234567890", "mah")})
        self.assertNotEqual(_measurements("2 года"), _measurements(2, "шт"))


if __name__ == "__main__":
    unittest.main()
