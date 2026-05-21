"""
Unit tests for SKU normalization and fuzzy matching.

Run:  python -m pytest tests/test_sku_matching.py -v
"""
import pytest

from merge_products import (
    normalize_sku,
    are_skus_similar,
    _levenshtein_ratio,
    _are_normalized_similar,
    find_qualifying_skus,
)


# ========================================================================
#  normalize_sku
# ========================================================================
class TestNormalizeSku:
    """Symbol removal + uppercasing."""

    def test_basic(self):
        assert normalize_sku("AB-123_x") == "AB123X"

    def test_spaces_and_slashes(self):
        assert normalize_sku("  hp/15s ") == "HP15S"

    def test_dots_and_parens(self):
        assert normalize_sku("SKU.(v2)") == "SKUV2"

    def test_already_clean(self):
        assert normalize_sku("ABC123") == "ABC123"

    def test_lowercase_output(self):
        assert normalize_sku("abc123") == "ABC123"

    def test_empty_string(self):
        assert normalize_sku("") == ""

    def test_none(self):
        assert normalize_sku(None) == ""

    def test_only_symbols(self):
        assert normalize_sku("---///___") == ""


# ========================================================================
#  are_skus_similar – exact & substring matches
# ========================================================================
class TestAreSkusSimilarExact:
    """Case-insensitive / symbol-insensitive exact matches."""

    def test_identical(self):
        assert are_skus_similar("GA-401QM", "GA-401QM")

    def test_case_insensitive(self):
        assert are_skus_similar("ga-401qm", "GA-401QM")

    def test_symbol_difference(self):
        assert are_skus_similar("GA_401-QM", "GA.401.QM")

    def test_spaces_vs_dashes(self):
        assert are_skus_similar("AB 123 X", "AB-123-X")


# ========================================================================
#  are_skus_similar – prefix / suffix additions
# ========================================================================
class TestAreSkusSimilarPrefixSuffix:
    """Shops may add extra characters at start or end."""

    def test_suffix_added(self):
        # "GA401QM" (7) inside "GA401QMX" (8), ratio 7/8 = 0.875 >= 0.85
        assert are_skus_similar("GA-401QM", "GA-401QMX")

    def test_prefix_added(self):
        # "GA401QM" (7) inside "XGA401QM" (8), ratio 7/8 = 0.875 >= 0.85
        assert are_skus_similar("GA-401QM", "X-GA-401QM")

    def test_prefix_too_long(self):
        # "GA401QM" (7) inside "XXXGA401QM" (10), ratio 7/10 = 0.7 < 0.85
        # Levenshtein ratio also low → should NOT match
        assert not are_skus_similar("GA-401QM", "XXX-GA-401QM")

    def test_both_prefix_and_suffix(self):
        # "GA401QM" (7) inside "XGA401QMY" (9), ratio 7/9 = 0.778 < 0.85
        # They differ by 2 chars, Levenshtein ratio = 1 - 2/9 ≈ 0.78 < 0.85
        assert not are_skus_similar("GA-401QM", "X-GA-401QM-Y")

    def test_region_code_suffix(self):
        # Common: shop appends a 1-char region code
        # "LAPTOP12345" vs "LAPTOP12345T" -> 11/12 = 0.917 >= 0.85
        assert are_skus_similar("LAPTOP-12345", "LAPTOP-12345-T")


# ========================================================================
#  are_skus_similar – close but NOT matching (< 85 %)
# ========================================================================
class TestAreSkusSimilarNoMatch:
    """SKUs that look close but don't meet the 85 % threshold."""

    def test_different_products(self):
        assert not are_skus_similar("GA-401QM", "GA-502QM")

    def test_swapped_digits(self):
        # "GA401QM" vs "GA410QM" — 2 edits in 7 chars → 5/7 ≈ 0.71
        assert not are_skus_similar("GA-401QM", "GA-410QM")

    def test_completely_different(self):
        assert not are_skus_similar("ABCDEFGH", "12345678")

    def test_different_length_below_ratio(self):
        assert not are_skus_similar("ABC123", "ABC123XYZWW")


# ========================================================================
#  are_skus_similar – short SKU behavior (len < 5)
# ========================================================================
class TestAreSkusSimilarShort:
    """Short normalized SKUs must match exactly."""

    def test_short_exact(self):
        assert are_skus_similar("AB12", "AB12")

    def test_short_case_insensitive(self):
        # Normalized form is the same
        assert are_skus_similar("ab12", "AB12")

    def test_short_off_by_one(self):
        # Even though Levenshtein ratio would be high, short → exact only
        assert not are_skus_similar("AB12", "AB13")

    def test_short_substring(self):
        # "AB1" (3) is short → requires exact, even though contained in "AB12"
        assert not are_skus_similar("AB1", "AB12")

    def test_four_chars_exact(self):
        assert are_skus_similar("A1B2", "A1-B2")

    def test_four_chars_not_exact(self):
        assert not are_skus_similar("A1B2", "A1B3")


# ========================================================================
#  are_skus_similar – null / empty edge cases
# ========================================================================
class TestAreSkusSimilarEdge:
    def test_both_none(self):
        assert not are_skus_similar(None, None)

    def test_one_none(self):
        assert not are_skus_similar("GA-401QM", None)

    def test_both_empty(self):
        assert not are_skus_similar("", "")

    def test_one_empty(self):
        assert not are_skus_similar("ABC123", "")

    def test_only_symbols(self):
        # Normalizes to "" → no match
        assert not are_skus_similar("---", "___")


# ========================================================================
#  _levenshtein_ratio sanity checks
# ========================================================================
class TestLevenshteinRatio:
    def test_identical(self):
        assert _levenshtein_ratio("ABC", "ABC") == 1.0

    def test_one_edit(self):
        # "ABC" vs "ABD" → distance 1, ratio = 1 - 1/3 ≈ 0.667
        assert abs(_levenshtein_ratio("ABC", "ABD") - 2 / 3) < 1e-9

    def test_empty_both(self):
        assert _levenshtein_ratio("", "") == 1.0

    def test_empty_one(self):
        assert _levenshtein_ratio("ABC", "") == 0.0


# ========================================================================
#  find_qualifying_skus (integration-level)
# ========================================================================
class TestFindQualifyingSkus:
    """Integration: verify fuzzy grouping across mock indexes."""

    @staticmethod
    def _make_product(sku: str):
        return {"sku": sku, "title": f"Product {sku}", "price": 100}

    def test_exact_match_across_shops(self):
        indexes = {
            "shop_a": {"GA-401QM": self._make_product("GA-401QM")},
            "shop_b": {"GA-401QM": self._make_product("GA-401QM")},
        }
        result = find_qualifying_skus(indexes, min_shops=2)
        assert len(result) == 1
        group = list(result.values())[0]
        assert set(group.keys()) == {"shop_a", "shop_b"}

    def test_case_symbol_difference(self):
        indexes = {
            "shop_a": {"GA-401QM": self._make_product("GA-401QM")},
            "shop_b": {"ga_401qm": self._make_product("ga_401qm")},
        }
        result = find_qualifying_skus(indexes, min_shops=2)
        assert len(result) == 1

    def test_suffix_fuzzy_match(self):
        indexes = {
            "shop_a": {"GA-401QM": self._make_product("GA-401QM")},
            "shop_b": {"GA-401QMX": self._make_product("GA-401QMX")},
        }
        result = find_qualifying_skus(indexes, min_shops=2)
        assert len(result) == 1

    def test_no_match_different_products(self):
        indexes = {
            "shop_a": {"GA-401QM": self._make_product("GA-401QM")},
            "shop_b": {"GA-502QM": self._make_product("GA-502QM")},
        }
        result = find_qualifying_skus(indexes, min_shops=2)
        assert len(result) == 0

    def test_short_sku_exact_only(self):
        indexes = {
            "shop_a": {"AB12": self._make_product("AB12")},
            "shop_b": {"AB13": self._make_product("AB13")},
        }
        result = find_qualifying_skus(indexes, min_shops=2)
        assert len(result) == 0

    def test_min_shops_filter(self):
        indexes = {
            "shop_a": {"SKU-12345": self._make_product("SKU-12345")},
            "shop_b": {"SKU-99999": self._make_product("SKU-99999")},
            "shop_c": {"SKU-12345": self._make_product("SKU-12345")},
        }
        result = find_qualifying_skus(indexes, min_shops=2)
        # SKU-12345 is in 2 shops, SKU-99999 in 1
        assert len(result) == 1
        group = list(result.values())[0]
        assert "shop_a" in group and "shop_c" in group
