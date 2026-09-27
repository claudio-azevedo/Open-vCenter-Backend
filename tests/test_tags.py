from __future__ import annotations

import pytest

from app.api.errors import ApiError
from app.services.tags import check_tag_color, check_tag_name


@pytest.mark.parametrize("name", ["Windows", "Datacenter-1", "dc_2", "A", "x" * 64, " Linux "])
def test_valid_tag_names(name: str) -> None:
    assert check_tag_name("Tag", name) == name.strip()


@pytest.mark.parametrize(
    "name", ["", "   ", "Datacenter 1", "OS:Win", "café", "a.b", "x" * 65, "tab\tname"]
)
def test_invalid_tag_names(name: str) -> None:
    with pytest.raises(ApiError) as exc:
        check_tag_name("Tag", name)
    assert exc.value.code == "INVALID"
    assert exc.value.status_code == 400


@pytest.mark.parametrize("color", ["gray", "red", "navy", "pink"])
def test_valid_tag_colors(color: str) -> None:
    assert check_tag_color(color) == color


@pytest.mark.parametrize("color", ["", "Red", "#ff0000", "magenta"])
def test_invalid_tag_colors(color: str) -> None:
    with pytest.raises(ApiError) as exc:
        check_tag_color(color)
    assert exc.value.code == "INVALID"
