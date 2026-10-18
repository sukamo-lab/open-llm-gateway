import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.release_check import check_metadata, compare, write_checksums


ROOT = Path(__file__).resolve().parents[1]


def test_repository_release_metadata_is_consistent():
    check_metadata(None)
    with pytest.raises(ValueError, match="must equal"):
        check_metadata("v999.0.0")


def test_branch_ref_is_not_implicitly_treated_as_release_tag():
    env = os.environ.copy()
    env["GITHUB_REF_NAME"] = "codex/release-v0.9.0"
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/release_check.py"), "metadata"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


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
