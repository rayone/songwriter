"""Build metadata must identify the server process, not the latest disk checkout."""
from unittest.mock import patch

from songwriter.web.app import BUILD_INFO, index, running_version


def test_ui_displays_running_build_without_javascript():
    response = index()
    page = response.body.decode()
    assert 'class="build-version"' in page
    assert 'href="/api/version"' in page
    assert str(BUILD_INFO['label']) in page
    assert '__SONGWRITER_BUILD_' not in page
    assert response.headers['cache-control'] == 'no-store'


def test_version_endpoint_returns_startup_snapshot():
    import json
    with patch('songwriter.build_info.collect_build_info', side_effect=AssertionError('must not re-read Git')):
        response = running_version()
    assert json.loads(response.body) == BUILD_INFO
    assert response.headers['cache-control'] == 'no-store'


def test_build_metadata_is_html_escaped():
    with patch.dict(BUILD_INFO, {'label': '<script>bad()</script>', 'branch': '" onmouseover="bad()'}):
        page = index().body.decode()
    assert '<script>bad()</script>' not in page
    assert '&lt;script&gt;bad()&lt;/script&gt;' in page
    assert '&quot; onmouseover=&quot;bad()' in page
