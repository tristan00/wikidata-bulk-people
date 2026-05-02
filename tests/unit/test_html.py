"""Unit tests for HTML helpers in _util.py and RestClient.extract_image_alts."""

from wikidata_bulk_people._clients import RestClient
from wikidata_bulk_people._util import _filename_from_upload_url, _strip_html


class TestStripHtml:
    def test_removes_tags(self) -> None:
        assert _strip_html("<b>Hello</b> <i>world</i>") == "Hello world"

    def test_normalises_whitespace(self) -> None:
        assert _strip_html("Hello   \n  world") == "Hello world"

    def test_empty_string_returns_none(self) -> None:
        assert _strip_html("") is None

    def test_none_input_returns_none(self) -> None:
        assert _strip_html(None) is None

    def test_whitespace_only_returns_none(self) -> None:
        assert _strip_html("   ") is None

    def test_nested_tags(self) -> None:
        result = _strip_html("<div><p>Hello <span>world</span></p></div>")
        assert result == "Hello world"


class TestFilenameFromUploadUrl:
    def test_direct_commons_url(self) -> None:
        url = "https://upload.wikimedia.org/wikipedia/commons/a/ab/Einstein_1921.jpg"
        assert _filename_from_upload_url(url) == "Einstein_1921.jpg"

    def test_thumb_url(self) -> None:
        url = (
            "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/"
            "Einstein_1921.jpg/300px-Einstein_1921.jpg"
        )
        assert _filename_from_upload_url(url) == "Einstein_1921.jpg"

    def test_svg_thumb_url(self) -> None:
        url = (
            "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/"
            "Flag_of_France.svg/300px-Flag_of_France.svg.png"
        )
        assert _filename_from_upload_url(url) == "Flag_of_France.svg"

    def test_unrecognised_url_returns_none(self) -> None:
        assert _filename_from_upload_url("https://example.com/image.jpg") is None

    def test_empty_url_returns_none(self) -> None:
        assert _filename_from_upload_url("") is None


class TestExtractImageAlts:
    def _make_client(self, html: str | None, status: int = 200) -> RestClient:
        from unittest.mock import MagicMock

        client = RestClient(user_agent="test/1.0")
        mock_resp = MagicMock()
        mock_resp.status_code = status
        mock_resp.text = html or ""
        client._session = MagicMock()  # type: ignore[assignment]
        client._session.get.return_value = mock_resp
        return client

    def test_extracts_alts_from_html(self) -> None:
        html = (
            '<img src="//upload.wikimedia.org/wikipedia/commons/thumb/a/ab/'
            'Einstein_1921.jpg/300px-Einstein_1921.jpg" alt="Young Einstein">'
            '<img src="//upload.wikimedia.org/wikipedia/commons/thumb/b/bc/'
            'Curie_1920.jpg/300px-Curie_1920.jpg" alt="Marie Curie">'
        )
        alts = self._make_client(html).extract_image_alts("Albert Einstein")
        assert alts.get("einstein_1921.jpg") == "Young Einstein"
        assert alts.get("curie_1920.jpg") == "Marie Curie"

    def test_image_without_alt_omitted(self) -> None:
        html = (
            '<img src="//upload.wikimedia.org/wikipedia/commons/thumb/a/ab/'
            'Photo.jpg/300px-Photo.jpg">'
        )
        alts = self._make_client(html).extract_image_alts("Test")
        assert alts == {}

    def test_http_error_returns_empty(self) -> None:
        alts = self._make_client(None, status=404).extract_image_alts("Nonexistent Page")
        assert alts == {}
