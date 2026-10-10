from __future__ import annotations

import argparse
import re
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent


def update_version(
    path: Path,
    resource_version: str,
    table_version: str,
) -> None:

    text = path.read_text(
        encoding="utf-8"
    )

    text = re.sub(
        r"(?im)^\s*ResourceVersion\s*:\s*\d+\s*$",
        f"ResourceVersion: {resource_version}",
        text,
        count=1,
    )

    text = re.sub(
        r"(?im)^\s*TableVersion\s*:\s*\d+\s*$",
        f"TableVersion: {table_version}",
        text,
        count=1,
    )

    path.write_text(
        text,
        encoding="utf-8",
    )


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--resource-version",
        required=True,
    )

    parser.add_argument(
        "--table-version",
        required=True,
    )

    parser.add_argument(
        "--file",
        default="current.txt",
    )

    args = parser.parse_args()

    path = Path(args.file)

    if not path.is_absolute():
        path = ROOT_DIR / path

    if not path.exists():
        raise FileNotFoundError(
            f"找不到 current.txt: {path}"
        )

    update_version(
        path,
        args.resource_version,
        args.table_version,
    )

    print(
        f"ResourceVersion -> {args.resource_version}"
    )
    print(
        f"TableVersion -> {args.table_version}"
    )


if __name__ == "__main__":
    main()
