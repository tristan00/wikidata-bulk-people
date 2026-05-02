"""Unit tests for image chrome-detection helper in _util.py."""

import pytest

from wikidata_bulk_people._util import _looks_like_chrome


class TestLooksLikeChrome:
    @pytest.mark.parametrize(
        "filename",
        [
            "Commons-logo.svg",
            "Wikidata-logo.svg",
            "Flag_of_the_United_States.svg",
            "Flag_of_France.svg",
            "OOjs_UI_icon_edit.svg",
            "Edit-clear.svg",
            "Gnome-edit-clear.svg",
            "Question_mark.svg",
            "Nuvola_apps_important.svg",
            "Padlock-silver-medium.svg",
            "Disambig_gray.svg",
            "Symbol_support_vote.svg",
            "Ambox_warning_pn.svg",
            "Crystal_Clear_app_error.png",
            "Red_Pencil_Icon.png",
            "Portal-puzzle.svg",
            "WPanthroponymy.svg",
            "P_vip.svg",
            "Coat_of_arms_placeholder.png",
            "Map_of_something.png",
            "Noimage.png",
            "No_image.svg",
        ],
    )
    def test_chrome_files_detected(self, filename: str) -> None:
        assert _looks_like_chrome(filename) is True, f"Expected chrome: {filename}"

    @pytest.mark.parametrize(
        "filename",
        [
            "Albert_Einstein_1921.jpg",
            "Marie_Curie_c1920.jpg",
            "Einstein_1905.jpg",
            "Newton_portrait.jpg",
            "Darwin_aged_51.jpg",
            "Lincoln_1863.jpg",
        ],
    )
    def test_portrait_files_not_chrome(self, filename: str) -> None:
        assert _looks_like_chrome(filename) is False, f"Unexpected chrome: {filename}"

    @pytest.mark.parametrize(
        "filename",
        [
            "something.pdf",
            "audio.ogg",
            "video.webm",
            "document.djvu",
            "spreadsheet.xlsx",
        ],
    )
    def test_non_image_extensions_are_chrome(self, filename: str) -> None:
        assert _looks_like_chrome(filename) is True, f"Expected non-image filtered: {filename}"
