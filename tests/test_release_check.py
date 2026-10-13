from pathlib import Path

import pytest

from scripts.release_check import check_metadata, compare, write_checksums


def test_repository_release_metadata_is_consistent():
    check_metadata(None)
    with pytest.raises(ValueError, match="must equal"):
        check_metadata("v999.0.0")


def test_reproducibility_comparison_and_stable_checksums(tmp_path: Path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "package.whl").write_bytes(b"same artifact")
    (second / "package.whl").write_bytes(b"same artifact")
    compare(first, second)

    checksum_file = first / "SHA256SUMS"
    write_checksums(first, checksum_file)
    original = checksum_file.read_text(encoding="utf-8")
    write_checksums(first, checksum_file)
    assert checksum_file.read_text(encoding="utf-8") == original
    assert "package.whl" in original

    (second / "package.whl").write_bytes(b"different artifact")
    with pytest.raises(ValueError, match="not reproducible"):
        compare(first, second)
