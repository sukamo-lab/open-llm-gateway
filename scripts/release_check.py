from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import tomllib
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
VERSION_PATTERN = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']\s*$', re.MULTILINE)


def product_versions() -> dict[str, str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    source = (ROOT / "src/llm_gateway/__init__.py").read_text(encoding="utf-8")
    match = VERSION_PATTERN.search(source)
    if not match:
        raise ValueError("src/llm_gateway/__init__.py does not declare __version__")
    ledger = yaml.safe_load((ROOT / "docs/FEATURE_STATUS.yaml").read_text(encoding="utf-8"))
    return {
        "package": str(project["project"]["version"]),
        "runtime": match.group(1),
        "ledger": str(ledger["product_version"]),
    }


def check_metadata(tag: str | None) -> None:
    versions = product_versions()
    if len(set(versions.values())) != 1:
        raise ValueError(f"product versions disagree: {versions}")
    version = versions["package"]
    expected_tag = f"v{version}"
    if tag and tag != expected_tag:
        raise ValueError(f"release tag {tag!r} must equal {expected_tag!r}")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    if not re.search(rf"^## \[{re.escape(version)}\](?:\s+-\s+\d{{4}}-\d{{2}}-\d{{2}})?\s*$", changelog, re.MULTILINE):
        raise ValueError(f"CHANGELOG.md has no release heading for {version}")
    unreleased = re.search(
        r"^## \[Unreleased\]\s*(.*?)(?=^## \[|\Z)", changelog, re.MULTILINE | re.DOTALL
    )
    if tag and unreleased and re.search(r"^\s*-\s+", unreleased.group(1), re.MULTILINE):
        raise ValueError("CHANGELOG.md still has entries under Unreleased")
    ledger = yaml.safe_load((ROOT / "docs/FEATURE_STATUS.yaml").read_text(encoding="utf-8"))
    missing = []
    for feature in ledger["features"]:
        if feature["status"] in {"implemented", "partial"}:
            for evidence in feature.get("evidence", []):
                if not (ROOT / evidence).exists():
                    missing.append(f"{feature['id']}: {evidence}")
    if missing:
        raise ValueError("feature evidence is missing:\n" + "\n".join(missing))
    print(f"release metadata valid: {version} ({expected_tag})")


def hashes(directory: Path) -> dict[str, str]:
    if not directory.is_dir():
        raise ValueError(f"artifact directory does not exist: {directory}")
    result = {}
    for path in sorted(item for item in directory.iterdir() if item.is_file()):
        result[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not result:
        raise ValueError(f"artifact directory is empty: {directory}")
    return result


def compare(first: Path, second: Path) -> None:
    first_hashes, second_hashes = hashes(first), hashes(second)
    if first_hashes != second_hashes:
        names = sorted(set(first_hashes) | set(second_hashes))
        differences = [
            name for name in names if first_hashes.get(name) != second_hashes.get(name)
        ]
        raise ValueError(f"builds are not reproducible; differing artifacts: {differences}")
    for name, digest in first_hashes.items():
        print(f"reproducible {digest}  {name}")


def write_checksums(directory: Path, output: Path) -> None:
    artifact_hashes = hashes(directory)
    artifact_hashes.pop(output.name, None)
    if not artifact_hashes:
        raise ValueError("no release artifacts remain after excluding the checksum file")
    lines = [f"{digest}  {name}" for name, digest in artifact_hashes.items()]
    output.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate and compare release artifacts")
    commands = parser.add_subparsers(dest="command", required=True)
    metadata = commands.add_parser("metadata")
    metadata.add_argument("--tag", default=os.getenv("GITHUB_REF_NAME"))
    compare_parser = commands.add_parser("compare")
    compare_parser.add_argument("first", type=Path)
    compare_parser.add_argument("second", type=Path)
    checksums = commands.add_parser("checksums")
    checksums.add_argument("directory", type=Path)
    checksums.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "metadata":
            check_metadata(args.tag)
        elif args.command == "compare":
            compare(args.first, args.second)
        else:
            write_checksums(args.directory, args.output)
    except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError) as exc:
        print(f"release validation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
