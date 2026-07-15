from pathlib import Path

import pytest

from tools.download_latest_renderdoc import _safe_destination, latest_version


def test_latest_version_uses_numeric_ordering():
    html = "/stable/1.9/renderdoc_1.9.tar.gz /stable/1.45/renderdoc_1.45.tar.gz"
    assert latest_version(html) == "1.45"


def test_archive_member_must_remain_below_destination(tmp_path: Path):
    with pytest.raises(RuntimeError, match="escapes destination"):
        _safe_destination(tmp_path, "../outside")
