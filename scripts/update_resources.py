from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================================================
# 路径
# ============================================================

ROOT_DIR = Path(__file__).resolve().parent.parent

SCRIPTS_DIR = ROOT_DIR / "scripts"

MODIFIED_DIR = ROOT_DIR / "modified"
OUT_DIR = ROOT_DIR / "out"

CURRENT_TXT = ROOT_DIR / "current.txt"

EXCLUSIONS_DIR = ROOT_DIR / "assetexclusions"

# ============================================================
# URL
# ============================================================

OLD_BUNDLE_INFO_BASE = (
    "https://mx.infastra.de5.net"
    "/prodm39/AssetBundles/Catalog"
)

OFFICIAL_BUNDLE_INFO_BASE = (
    "https://static.bluearchive-cn.com"
    "/prodm39/AssetBundles/Catalog"
)

OFFICIAL_BUNDLE_BASE = (
    "https://static.bluearchive-cn.com"
    "/prodm39/AssetBundles/Android"
)

OFFICIAL_TABLE_MANIFEST_BASE = (
    "https://static.bluearchive-cn.com"
    "/prodm39/Manifest/TableBundles"
)

OFFICIAL_TABLE_BUNDLE_BASE = (
    "https://static.bluearchive-cn.com"
    "/prodm39/pool/TableBundles"
)

OLD_EXCEL_DB_URL = (
    "https://mx.infastra.de5.net"
    "/prodm39/pool/TableBundles/51/"
    "517bb1fb1aa4d980ab87644ec10ecb2a"
)


# ============================================================
# HTTP Session
# ============================================================

def create_session() -> requests.Session:
    session = requests.Session()

    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=1,
        status_forcelist=(
            429,
            500,
            502,
            503,
            504,
        ),
        allowed_methods=frozenset({"GET"}),
    )

    adapter = HTTPAdapter(
        max_retries=retry
    )

    session.mount("https://", adapter)
    session.mount("http://", adapter)

    return session


SESSION = create_session()


# ============================================================
# 下载
# ============================================================

def download_bytes(
    url: str,
    output: Path,
    max_attempts: int = 6,
) -> None:

    print(f"[DOWNLOAD] {url}")

    output.parent.mkdir(parents=True, exist_ok=True)

    tmp = output.with_name(output.name + ".part")

    if tmp.exists():
        tmp.unlink()

    last_exc: Exception | None = None

    for attempt in range(1, max_attempts + 1):

        resume_from = tmp.stat().st_size if tmp.exists() else 0

        # identity: 避免压缩导致 Content-Length 与实际字节数对不上
        headers = {"Accept-Encoding": "identity"}

        if resume_from > 0:
            headers["Range"] = f"bytes={resume_from}-"

        try:
            with SESSION.get(
                url,
                headers=headers,
                stream=True,
                timeout=(15, 120),
            ) as response:

                if response.status_code == 416:
                    # Range 不合法，丢弃残留重新下载
                    tmp.unlink(missing_ok=True)
                    raise RuntimeError("416 Range Not Satisfiable")

                response.raise_for_status()

                if response.status_code == 206:
                    mode = "ab"
                else:
                    # 200：服务器不支持 Range 或首次下载，从头写
                    mode = "wb"
                    resume_from = 0

                content_length = response.headers.get("Content-Length")
                expected = (
                    resume_from + int(content_length)
                    if content_length is not None
                    else None
                )

                with tmp.open(mode) as f:
                    for chunk in response.iter_content(
                        chunk_size=1024 * 1024
                    ):
                        if chunk:
                            f.write(chunk)

            actual = tmp.stat().st_size

            if expected is not None and actual != expected:
                raise RuntimeError(
                    f"文件大小不一致: expected={expected}, actual={actual}"
                )

            tmp.replace(output)
            return

        except Exception as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)
            print(
                f"  [RETRY {attempt}/{max_attempts}] "
                f"{type(exc).__name__}: {exc}；{wait}s 后重试"
            )
            time.sleep(wait)

    tmp.unlink(missing_ok=True)

    raise RuntimeError(
        f"下载失败（已重试 {max_attempts} 次）: {url}"
    ) from last_exc


def download_json(
    url: str,
    output: Path,
) -> dict:

    download_bytes(
        url,
        output,
    )

    with output.open(
        "r",
        encoding="utf-8",
    ) as f:
        return json.load(f)


def save_json(
    path: Path,
    data: dict,
) -> None:

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with path.open(
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2,
        )

        f.write("\n")


# ============================================================
# current.txt
# ============================================================

def read_current_txt() -> tuple[str, str]:

    if not CURRENT_TXT.exists():
        raise FileNotFoundError(
            f"找不到 current.txt: {CURRENT_TXT}"
        )

    text = CURRENT_TXT.read_text(
        encoding="utf-8"
    )

    resource_match = re.search(
        r"(?im)^\s*ResourceVersion\s*[:=]\s*(\d+)\s*$",
        text,
    )

    table_match = re.search(
        r"(?im)^\s*TableVersion\s*[:=]\s*(\d+)\s*$",
        text,
    )

    if resource_match is None:
        raise RuntimeError(
            "current.txt 中找不到 ResourceVersion"
        )

    if table_match is None:
        raise RuntimeError(
            "current.txt 中找不到 TableVersion"
        )

    return (
        resource_match.group(1),
        table_match.group(1),
    )


# ============================================================
# BundleDownloadInfo
# ============================================================

def build_bundle_map(
    data: dict,
) -> dict[str, dict]:

    entries = data.get("BundleFiles")

    if not isinstance(entries, list):
        raise RuntimeError(
            "bundleDownloadInfo.json 中不存在 BundleFiles"
        )

    result: dict[str, dict] = {}

    for entry in entries:

        name = entry.get("Name")

        if not name:
            continue

        if name in result:
            raise RuntimeError(
                f"发现重复 Bundle Name: {name}"
            )

        result[name] = entry

    return result


def find_bundle_differences(
    old_data: dict,
    new_data: dict,
) -> list[dict]:

    old_map = build_bundle_map(old_data)
    new_map = build_bundle_map(new_data)

    differences = []

    for name, new_entry in new_map.items():

        if not name.lower().endswith(".bundle"):
            continue

        new_crc = str(
            new_entry.get("Crc", "")
        ).lower()

        old_entry = old_map.get(name)

        # ----------------------------------------------
        # 旧版本不存在
        # ----------------------------------------------

        if old_entry is None:

            print(
                f"[DIFF] 新增 Bundle: {name}"
            )

            differences.append(
                new_entry
            )

            continue

        # ----------------------------------------------
        # CRC 变化
        # ----------------------------------------------

        old_crc = str(
            old_entry.get("Crc", "")
        ).lower()

        if old_crc != new_crc:

            print(
                f"[DIFF] CRC 改变: {name}"
            )

            print(
                f"       old = {old_crc}"
            )

            print(
                f"       new = {new_crc}"
            )

            differences.append(
                new_entry
            )

    return differences


# ============================================================
# 运行脚本
# ============================================================

def run_script(
    script_name: str,
) -> None:

    script = SCRIPTS_DIR / script_name

    if not script.exists():
        raise FileNotFoundError(
            f"找不到脚本: {script}"
        )

    print()
    print("=" * 72)
    print(f"[RUN] {script}")
    print("=" * 72)

    subprocess.run(
        [
            sys.executable,
            str(script),
        ],
        cwd=ROOT_DIR,
        check=True,
    )


# ============================================================
# 随机数字
# ============================================================

def random_digits(
    length: int,
) -> str:

    return str(
        random.SystemRandom().randint(
            10 ** (length - 1),
            10 ** length - 1,
        )
    )


# ============================================================
# 清理上一次的生成文件
# ============================================================

def clean_workspace() -> None:

    print("[CLEAN] 清理 generated workspace")

    for path in (
        MODIFIED_DIR,
        OUT_DIR,
    ):

        if path.exists():
            shutil.rmtree(path)

    for path in ROOT_DIR.glob("*.bundle"):

        if path.is_file():
            path.unlink()

    for filename in (
        "ExcelDB_new.db",
        "ExcelDB_old.db",
        "ExcelDB_new_backup.db",
    ):

        path = ROOT_DIR / filename

        if path.exists():
            path.unlink()


# ============================================================
# 删除临时文件
# ============================================================

def cleanup_temp_files() -> None:

    print("[CLEAN] 清理临时文件")

    for path in ROOT_DIR.glob("*.bundle"):

        if path.is_file():
            path.unlink()

    for filename in (
        "ExcelDB_new.db",
        "ExcelDB_old.db",
        "ExcelDB_new_backup.db",
    ):

        path = ROOT_DIR / filename

        if path.exists():
            path.unlink()


# ============================================================
# 把 out/ Bundle 移到 modified/AssetBundles/Android
# ============================================================

def copy_modified_bundles() -> int:

    source_dir = OUT_DIR

    destination_dir = (
        MODIFIED_DIR
        / "AssetBundles"
        / "Android"
    )

    if not source_dir.exists():
        return 0

    bundle_files = sorted(
        source_dir.rglob("*.bundle")
    )

    if not bundle_files:
        return 0

    destination_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    count = 0

    for source in bundle_files:

        # replaceTexture2D 输出中理论上只有文件名，
        # 但仍然保留 relative path 的能力。
        relative = source.relative_to(
            source_dir
        )

        target = (
            destination_dir / relative
        )

        target.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        shutil.copy2(
            source,
            target,
        )

        print(
            f"[COPY] {source} -> {target}"
        )

        count += 1

    return count


def find_exclusion_bundles() -> dict[str, Path]:
    """返回 {文件名: 路径}。文件名重复直接报错。"""

    result: dict[str, Path] = {}

    if not EXCLUSIONS_DIR.is_dir():
        return result

    for path in sorted(EXCLUSIONS_DIR.rglob("*.bundle")):

        if not path.is_file():
            continue

        if path.name in result:
            raise RuntimeError(
                f"AssetExclusions 中存在重名 Bundle: {path.name}\n"
                f"  {result[path.name]}\n"
                f"  {path}"
            )

        result[path.name] = path

    return result


def copy_exclusion_bundles(
    exclusions: dict[str, Path],
) -> int:

    if not exclusions:
        return 0

    destination_dir = (
        MODIFIED_DIR
        / "AssetBundles"
        / "Android"
    )

    destination_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    for name, source in exclusions.items():

        target = destination_dir / name

        shutil.copy2(source, target)

        print(f"[COPY-EXCLUSION] {source} -> {target}")

    return len(exclusions)

# ============================================================
# 主流程
# ============================================================

def main() -> int:

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--current-version-file",
        required=True,
    )

    parser.add_argument(
        "--github-output",
        action="store_true",
    )

    args = parser.parse_args()

    current_version_file = Path(
        args.current_version_file
    )

    if not current_version_file.is_absolute():
        current_version_file = (
            ROOT_DIR / current_version_file
        )

    # ========================================================
    # 读取当前官方版本
    # ========================================================

    with current_version_file.open(
        "r",
        encoding="utf-8",
    ) as f:
        current_info = json.load(f)

    official_resource_version = str(
        current_info["ResourceVersion"]
    )

    official_table_version = str(
        current_info["TableVersion"]
    )

    # ========================================================
    # 读取仓库记录版本
    # ========================================================

    repository_resource_version, repository_table_version = (
        read_current_txt()
    )

    print()
    print(
        f"Repository ResourceVersion: "
        f"{repository_resource_version}"
    )

    print(
        f"Official   ResourceVersion: "
        f"{official_resource_version}"
    )

    print(
        f"Repository TableVersion: "
        f"{repository_table_version}"
    )

    print(
        f"Official   TableVersion: "
        f"{official_table_version}"
    )

    # ========================================================
    # 判断是否更新
    # ========================================================

    changed = (
        repository_resource_version
        != official_resource_version
        or
        repository_table_version
        != official_table_version
    )

    if not changed:

        print()
        print(
            "ResourceVersion 和 TableVersion 均未变化。"
        )

        if args.github_output:

            output = os.environ.get(
                "GITHUB_OUTPUT"
            )

            if output:

                with open(
                    output,
                    "a",
                    encoding="utf-8",
                ) as f:
                    f.write("changed=false\n")

        return 0

    if args.github_output:

        output = os.environ.get(
            "GITHUB_OUTPUT"
        )

        if output:

            with open(
                output,
                "a",
                encoding="utf-8",
            ) as f:
                f.write("changed=true\n")

    # ========================================================
    # 开始更新
    # ========================================================

    clean_workspace()

    # ========================================================
    # 1. bundleDownloadInfo.json
    # ========================================================

    catalog_dir = (
        MODIFIED_DIR
        / "AssetBundles"
        / "Catalog"
        / official_resource_version
        / "Android"
    )

    catalog_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    old_catalog_path = (
        ROOT_DIR
        / ".old_bundleDownloadInfo.json"
    )

    official_catalog_path = (
        catalog_dir
        / "bundleDownloadInfo.json"
    )

    old_catalog_url = (
        f"{OLD_BUNDLE_INFO_BASE}/"
        f"{repository_resource_version}/"
        "Android/bundleDownloadInfo.json"
    )

    official_catalog_url = (
        f"{OFFICIAL_BUNDLE_INFO_BASE}/"
        f"{official_resource_version}/"
        "Android/bundleDownloadInfo.json"
    )

    try:
        old_catalog = download_json(
            old_catalog_url,
            old_catalog_path,
        )
    except Exception as e:
        print(f"下载旧版 Catalog 失败 ({old_catalog_url}): {e}")
        print("使用空 Catalog 进行对比，强制全量构建...")
        old_catalog = {"BundleFiles": []}

    official_catalog = download_json(
        official_catalog_url,
        official_catalog_path,
    )

    differences = find_bundle_differences(
        old_catalog,
        official_catalog,
    )

    exclusions = find_exclusion_bundles()

    official_names = set(build_bundle_map(official_catalog))

    stale = [n for n in exclusions if n not in official_names]

    if stale:
        raise RuntimeError(
            "AssetExclusions 中的以下 Bundle 不在官方 catalog 中，"
            "可能官方已更新，请更换文件:\n  "
            + "\n  ".join(stale)
        )

    print(f"AssetExclusions Bundle 数量: {len(exclusions)}")

    print()
    print(
        f"共发现 {len(differences)} 个发生变化的 Bundle"
    )

    # ========================================================
    # 2. 下载发生变化的 Bundle
    # ========================================================

    downloaded_count = 0

    for entry in differences:

        name = entry["Name"]

        # 防止 Name 带路径
        if Path(name).name != name:
            raise RuntimeError(
                f"Bundle Name 包含路径: {name}"
            )
        
        if name in exclusions:
            print(f"[SKIP-DOWNLOAD] {name} 在 AssetExclusions 中")
            continue        

        output = ROOT_DIR / name

        url = (
            f"{OFFICIAL_BUNDLE_BASE}/"
            f"{name}"
        )

        download_bytes(
            url,
            output,
        )

        downloaded_count += 1

    # ========================================================
    # 3. replaceTexture2D.py
    # ========================================================
    #
    # 只有在根目录真的有新的 .bundle 时才需要跑。
    # 官方只更新 TableVersion 时 differences 为 0（或者差异项全在
    # AssetExclusions 里），根目录下一个 .bundle 都没有，
    # replaceTexture2D.py 会以「没有找到任何 .bundle」退出码 1 中断整条流水线。
    # ========================================================

    if downloaded_count > 0:

        run_script(
            "replaceTexture2D.py"
        )

    else:

        print()
        print(
            "[SKIP] 没有下载到新的 Bundle，" 
            "跳过 replaceTexture2D.py"
        )

    # ========================================================
    # 4. calculateMD5.py
    # ========================================================
    #
    # 这一步不能跳过。
    # 它还会读取 assetexclusions/ 里的 Bundle，把它们真实的 MD5/Size
    # 写回 catalog；客户端的资源替换正是靠这些 Crc 记录生效的。
    # ========================================================

    run_script(
        "calculateMD5.py"
    )

    # ========================================================
    # 5. 将 out/ Bundle 复制到 modified
    # ========================================================

    modified_bundle_count = (
        copy_modified_bundles()
    )

    exclusion_count = copy_exclusion_bundles(exclusions)

    print(
        f"实际修改 Bundle: "
        f"{modified_bundle_count}"
    )

    print(f"直接复制 Bundle: {exclusion_count}")

    # ========================================================
    # 6. bundleDownloadInfo.hash
    # ========================================================

    bundle_hash_path = (
        catalog_dir
        / "bundleDownloadInfo.hash"
    )

    bundle_hash_path.write_text(
        random_digits(9),
        encoding="utf-8",
    )

    # ========================================================
    # 7. 下载 TableManifest
    # ========================================================

    table_manifest_dir = (
        MODIFIED_DIR
        / "Manifest"
        / "TableBundles"
        / official_table_version
    )

    table_manifest_path = (
        table_manifest_dir
        / "TableManifest"
    )

    table_manifest_url = (
        f"{OFFICIAL_TABLE_MANIFEST_BASE}/"
        f"{official_table_version}/"
        "TableManifest"
    )

    table_manifest = download_json(
        table_manifest_url,
        table_manifest_path,
    )

    # ========================================================
    # 8. 获取 ExcelDB.db CRC
    # ========================================================

    try:
        excel_entry = (
            table_manifest
            ["Table"]
            ["ExcelDB.db"]
        )
    except KeyError as exc:
        raise RuntimeError(
            "TableManifest 中找不到 Table -> ExcelDB.db"
        ) from exc

    excel_crc = str(
        excel_entry["Crc"]
    ).lower()

    print()
    print(
        f"官方 ExcelDB Crc: {excel_crc}"
    )

    # ========================================================
    # 9. 下载新的 ExcelDB
    # ========================================================

    excel_new_path = (
        ROOT_DIR
        / "ExcelDB_new.db"
    )

    official_excel_url = (
        f"{OFFICIAL_TABLE_BUNDLE_BASE}/"
        f"{excel_crc[:2]}/"
        f"{excel_crc}"
    )

    download_bytes(
        official_excel_url,
        excel_new_path,
    )

    # ========================================================
    # 10. 下载旧 ExcelDB (增加容错处理)
    # ========================================================

    excel_old_path = (
        ROOT_DIR
        / "ExcelDB_old.db"
    )

    has_old_excel = False
    try:
        download_bytes(
            OLD_EXCEL_DB_URL,
            excel_old_path,
        )
        has_old_excel = True
    except Exception as e:
        print(f"下载旧版 ExcelDB 失败: {e}")
        print("无法进行文本替换，将直接使用官方最新 ExcelDB。")

    # ========================================================
    # 11. updateExcelDB.py
    # ========================================================

    if has_old_excel:
        run_script(
            "updateExcelDB.py"
        )

        if not excel_new_path.exists():
            raise RuntimeError(
                "updateExcelDB.py 执行后 "
                "没有生成 ExcelDB_new.db"
            )
    else:
        print("跳过 updateExcelDB.py 执行。")

    # ========================================================
    # 12. 计算 ExcelDB MD5
    # ========================================================

    md5 = hashlib.md5()

    with excel_new_path.open("rb") as f:

        while True:

            chunk = f.read(
                1024 * 1024
            )

            if not chunk:
                break

            md5.update(chunk)

    excel_md5 = md5.hexdigest()

    excel_size = (
        excel_new_path.stat().st_size
    )

    print()
    print(
        f"ExcelDB MD5 : {excel_md5}"
    )

    print(
        f"ExcelDB Size : {excel_size}"
    )

    # ========================================================
    # 13. 移动 ExcelDB
    # ========================================================

    excel_target = (
        MODIFIED_DIR
        / "pool"
        / "TableBundles"
        / excel_md5[:2]
        / excel_md5
    )

    excel_target.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    shutil.move(
        str(excel_new_path),
        str(excel_target),
    )

    # ========================================================
    # 14. 修改 TableManifest
    # ========================================================

    excel_entry["Crc"] = excel_md5
    excel_entry["Size"] = excel_size

    save_json(
        table_manifest_path,
        table_manifest,
    )

    # ========================================================
    # 15. TableManifestHash
    # ========================================================

    table_manifest_hash_path = (
        table_manifest_dir
        / "TableManifestHash"
    )

    table_manifest_hash_path.write_text(
        random_digits(10),
        encoding="utf-8",
    )

    # ========================================================
    # 16. 清理临时文件
    # ========================================================

    cleanup_temp_files()

    if old_catalog_path.exists():
        old_catalog_path.unlink()

    print()
    print("=" * 72)
    print("资源修改完成")
    print("=" * 72)

    print(
        f"ResourceVersion : "
        f"{official_resource_version}"
    )

    print(
        f"TableVersion    : "
        f"{official_table_version}"
    )

    print(
        f"Bundle 差异     : "
        f"{len(differences)}"
    )

    print(
        f"修改 Bundle     : "
        f"{modified_bundle_count}"
    )

    print(
        f"ExcelDB MD5     : "
        f"{excel_md5}"
    )

    print("=" * 72)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
