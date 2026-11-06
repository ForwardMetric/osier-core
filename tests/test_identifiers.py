import pytest

from osier.errors import InvalidNameError
from osier.identifiers import Owner, suggest_name, validate_api_name


@pytest.mark.parametrize("name", ["c_region", "c_net_worth_2024", "c_a"])
def test_valid_custom_names(name):
    assert validate_api_name(name, kind="field", owner=Owner.CUSTOM) == name


@pytest.mark.parametrize(
    "name",
    [
        "region",  # custom names need the c_ prefix
        "C_region",
        "c_region;drop table x",
        'c_"quoted"',
        "c__double",
        "c_trailing_",
        "c_" + "x" * 47,  # too long
        "",
        "1c_starts_with_digit",
    ],
)
def test_invalid_custom_names(name):
    with pytest.raises(InvalidNameError):
        validate_api_name(name, kind="field", owner=Owner.CUSTOM)


@pytest.mark.parametrize("name", ["id", "created_at", "row_version", "owner_id"])
def test_system_columns_are_reserved(name):
    with pytest.raises(InvalidNameError):
        validate_api_name(name, kind="field", owner=Owner.STANDARD)


@pytest.mark.parametrize("name", ["select", "table", "meta_fields", "pg_class"])
def test_reserved_words_and_prefixes(name):
    with pytest.raises(InvalidNameError):
        validate_api_name(name, kind="object", owner=Owner.STANDARD)


def test_standard_names_may_not_use_custom_prefix():
    with pytest.raises(InvalidNameError):
        validate_api_name("c_name", kind="field", owner=Owner.STANDARD)


def test_error_suggests_a_valid_name():
    with pytest.raises(InvalidNameError) as exc:
        validate_api_name("Net Worth", kind="field", owner=Owner.CUSTOM)
    assert "c_net_worth" in exc.value.hint


def test_suggest_name():
    assert suggest_name("Net Worth (USD)", Owner.CUSTOM) == "c_net_worth_usd"
    assert suggest_name("2024 Revenue", Owner.STANDARD) == "x_2024_revenue"
