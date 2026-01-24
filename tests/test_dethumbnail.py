import pytest

from veracity.dethumbnail import get_full_res_url, _transform_reddit, _transform_twitter


class TestTransformReddit:
    def test_preview_redd_it_jpeg(self):
        # Takes last 13 chars of base filename
        url = "https://preview.redd.it/abc123def456g.jpeg"
        result = _transform_reddit(url)
        assert result == "https://i.redd.it/abc123def456g.jpeg"

    def test_preview_redd_it_jpg(self):
        url = "https://preview.redd.it/somefile12345.jpg"
        result = _transform_reddit(url)
        assert result == "https://i.redd.it/somefile12345.jpg"

    def test_preview_redd_it_truncates_long_filename(self):
        # Filename longer than 13 chars gets truncated to last 13
        url = "https://preview.redd.it/verylongfilename123.png"
        result = _transform_reddit(url)
        # "verylongfilename123"[-13:] = "ngfilename123"
        assert result == "https://i.redd.it/ngfilename123.png"

    def test_external_preview_redd_it(self):
        url = "https://external-preview.redd.it/xyz789abcdefg.webp"
        result = _transform_reddit(url)
        assert result == "https://i.redd.it/xyz789abcdefg.webp"

    def test_non_reddit_url_returns_none(self):
        url = "https://example.com/image.jpg"
        result = _transform_reddit(url)
        assert result is None

    def test_i_redd_it_returns_none(self):
        url = "https://i.redd.it/abc123.jpg"
        result = _transform_reddit(url)
        assert result is None


class TestTransformTwitter:
    def test_small_to_orig(self):
        url = "https://pbs.twimg.com/media/ABC123?format=jpg&name=small"
        result = _transform_twitter(url)
        assert result == "https://pbs.twimg.com/media/ABC123?format=jpg&name=orig"

    def test_medium_to_orig(self):
        url = "https://pbs.twimg.com/media/XYZ789?format=png&name=medium"
        result = _transform_twitter(url)
        assert result == "https://pbs.twimg.com/media/XYZ789?format=png&name=orig"

    def test_already_orig_returns_same(self):
        url = "https://pbs.twimg.com/media/ABC123?format=jpg&name=orig"
        result = _transform_twitter(url)
        # Returns same URL (get_full_res_url filters this out)
        assert result == "https://pbs.twimg.com/media/ABC123?format=jpg&name=orig"

    def test_no_format_defaults_to_jpg(self):
        url = "https://pbs.twimg.com/media/ABC123?name=small"
        result = _transform_twitter(url)
        assert result == "https://pbs.twimg.com/media/ABC123?format=jpg&name=orig"

    def test_non_twitter_url_returns_none(self):
        url = "https://example.com/media/image.jpg"
        result = _transform_twitter(url)
        assert result is None


class TestGetFullResUrl:
    def test_reddit_preview_upgrades(self):
        url = "https://preview.redd.it/abc123def456g.jpg"
        result = get_full_res_url(url)
        assert result is not None
        assert result.startswith("https://i.redd.it/")

    def test_twitter_thumbnail_upgrades(self):
        url = "https://pbs.twimg.com/media/ABC123?format=jpg&name=small"
        result = get_full_res_url(url)
        assert result == "https://pbs.twimg.com/media/ABC123?format=jpg&name=orig"

    def test_twitter_orig_returns_none(self):
        # Already at full res, should return None (not same URL)
        url = "https://pbs.twimg.com/media/ABC123?format=jpg&name=orig"
        result = get_full_res_url(url)
        assert result is None

    def test_regular_url_returns_none(self):
        url = "https://example.com/image.jpg"
        result = get_full_res_url(url)
        assert result is None

    def test_none_input_returns_none(self):
        result = get_full_res_url(None)
        assert result is None

    def test_empty_string_returns_none(self):
        result = get_full_res_url("")
        assert result is None
