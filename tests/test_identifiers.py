import pytest

from osier.errors import InvalidNameError
from osier.identifiers import (
    ENGINE_TABLES,
    SYSTEM_COLUMNS,
    Owner,
    suggest_name,
    validate_api_name,
)
from osier.packages import REGISTRY


@pytest.mark.parametrize("name", ["c__region", "c__net_worth_2024", "c__a"])
def test_valid_custom_names(name):
    assert validate_api_name(name, kind="field", owner=Owner.CUSTOM) == name


@pytest.mark.parametrize(
    "name",
    [
        "region",  # custom names need the c__ prefix
        "c_region",  # single underscore is not the custom prefix
        "C__region",
        "c__region;drop table x",
        'c__"quoted"',
        "c__",
        "c___triple",
        "c__double__inside",
        "c__trailing_",
        "c__1starts_with_digit",
        "c__" + "x" * 46,  # too long
        "",
    ],
)
def test_invalid_custom_names(name):
    with pytest.raises(InvalidNameError):
        validate_api_name(name, kind="field", owner=Owner.CUSTOM)


@pytest.mark.parametrize("name", ["c__name", "c_name", "contact__email", "name__x"])
def test_platform_names_cannot_look_custom_or_use_double_underscores(name):
    with pytest.raises(InvalidNameError):
        validate_api_name(name, kind="field", owner=Owner.STANDARD)


def test_platform_names_starting_with_c_but_not_prefix_are_fine():
    assert validate_api_name("currency", kind="field", owner=Owner.STANDARD) == "currency"
    assert validate_api_name("contact", kind="object", owner=Owner.STANDARD) == "contact"


@pytest.mark.parametrize("name", ["id", "created_at", "row_version", "owner_id"])
def test_system_columns_are_reserved(name):
    with pytest.raises(InvalidNameError):
        validate_api_name(name, kind="field", owner=Owner.STANDARD)


@pytest.mark.parametrize("name", ["select", "table", "meta_fields", "pg_class"])
def test_reserved_words_and_prefixes(name):
    with pytest.raises(InvalidNameError):
        validate_api_name(name, kind="object", owner=Owner.STANDARD)


def test_error_suggests_a_valid_name():
    with pytest.raises(InvalidNameError) as exc:
        validate_api_name("Net Worth", kind="field", owner=Owner.CUSTOM)
    assert "c__net_worth" in exc.value.hint
    with pytest.raises(InvalidNameError) as exc:
        validate_api_name("c_region", kind="field", owner=Owner.CUSTOM)
    assert "c__region" in exc.value.hint


def test_suggest_name():
    assert suggest_name("Net Worth (USD)", Owner.CUSTOM) == "c__net_worth_usd"
    assert suggest_name("c_region", Owner.CUSTOM) == "c__region"
    assert suggest_name("2024 Revenue", Owner.STANDARD) == "x_2024_revenue"


def test_no_platform_name_can_collide_with_the_custom_namespace():
    """System columns, engine tables and every package object/field stay outside 'c_'."""
    platform_names = set(SYSTEM_COLUMNS) | set(ENGINE_TABLES)
    for package in REGISTRY.values():
        for op in package.operations:
            platform_names.add(op["api_name"])
            platform_names.update(f["api_name"] for f in op["fields"])
    offenders = sorted(n for n in platform_names if n.startswith("c_") or "__" in n)
    assert offenders == []
