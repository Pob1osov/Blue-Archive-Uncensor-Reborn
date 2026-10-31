from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import boto3


ROOT_DIR = Path(__file__).resolve().parent.parent
MODIFIED_DIR = ROOT_DIR / "modified"
EXCLUSIONS_DIR = ROOT_DIR / "assetexclusions"

CATALOG_GLOB = ("AssetBundles", "Catalog", "*", "Android", "bundleDownloadInfo.json")
BUNDLE_KEY_PREFIX = "AssetBundles/Android/"

OFFICIAL_BUNDLE_BASE = (
    "https://static.bluearchive-cn.com"
    "/prodm39/AssetBundles/Android"
)

MAX_WORKERS = 2
MAX_ATTEMPTS = 4
RETRY_BASE_SECONDS = 2


def required_env(name: str) -> str:
    value = os.environ.get(name)

    if not value:
        raise RuntimeError(f"缺少环境变量: {name}")

    return value.strip()


def md5_of(path: Path) -> str:

    digest = hashlib.md5()

    with path.open("rb") as fh:

        while True:

            chunk = fh.read(1024 * 1024)

            if not chunk:
                break

            digest.update(chunk)

    return digest.hexdigest()


def find_catalog() -> Path:
    """modified/ 下的 bundleDownloadInfo.json。"""

    matches = sorted(MODIFIED_DIR.glob(str(Path(*CATALOG_GLOB))))

    if len(matches) != 1:

        raise RuntimeError(
            "期望恰好 1 个 bundleDownloadInfo.json，"
            f"实际找到 {len(matches)} 个"
        )

    return matches[0]


def make_session() -> requests.Session:

    session = requests.Session()

    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )

    adapter = HTTPAdapter(max_retries=retry)

    session.mount("https://", adapter)
    session.mount("http://", adapter)

    return session


def list_remote_sizes(client, bucket: str) -> dict[str, int]:
    """一次性把 R2 上所有对象的 key -> size 拉下来。

    list_objects_v2 自带 Size 字段，因此这里不需要对每个文件做
    head_object，2 万多个对象大约 25 次分页调用就能扫完。
    """

    sizes: dict[str, int] = {}

    paginator = client.get_paginator("list_objects_v2")

    for page in paginator.paginate(
        Bucket=bucket,
        Prefix=BUNDLE_KEY_PREFIX,
    ):

        for obj in page.get("Contents", []):

            key = obj.get("Key")

            if not key:
                continue

            sizes[key] = int(obj.get("Size", 0))

    return sizes


def download_official(
    session: requests.Session,
    name: str,
    dest: Path,
) -> None:

    url = f"{OFFICIAL_BUNDLE_BASE}/{name}"

    last_error = ""

    for attempt in range(1, MAX_ATTEMPTS + 1):

        try:

            with session.get(
                url,
                stream=True,
                timeout=(15, 180),
            ) as response:

                response.raise_for_status()

                expected = response.headers.get("Content-Length")

                with dest.open("wb") as fh:

                    for chunk in response.iter_content(
                        chunk_size=1024 * 1024
                    ):

                        if chunk:
                            fh.write(chunk)

            actual = dest.stat().st_size

            if expected is not None and actual != int(expected):

                raise RuntimeError(
                    f"下载不完整: expected={expected} actual={actual}"
                )

            return

        except Exception as exc:

            last_error = f"{type(exc).__name__}: {exc}"

            print(
                f"  [DL-RETRY {attempt}/{MAX_ATTEMPTS}] "
                f"{name}: {last_error}"
            )

            dest.unlink(missing_ok=True)

            if attempt < MAX_ATTEMPTS:

                import time

                time.sleep(min(RETRY_BASE_SECONDS ** attempt, 30))

    raise RuntimeError(f"下载失败: {name} ({last_error})")


def repair_one(
    client,
    bucket: str,
    session: requests.Session,
    entry: dict,
    transfer_config,
) -> tuple[str, bool, str]:

    """用一个正确来源覆盖 R2 上的错误对象。"""

    name = entry["Name"]
    expected_size = int(entry["Size"])
    expected_crc = str(entry.get("Crc", "")).lower()

    local_copy = EXCLUSIONS_DIR / name

    with tempfile.TemporaryDirectory() as tmp:

        if local_copy.is_file():

            source = local_copy
            origin = "assetexclusions"

        else:

            source = Path(tmp) / name
            origin = "official"

            try:

                download_official(session, name, source)

            except Exception as exc:
                return name, False, f"取源失败({origin}): {exc}"

        # 上传前先确认这份源文件本身就是对的，
        # 否则等于把错误内容再传一遍
        actual_size = source.stat().st_size
        actual_md5 = md5_of(source)

        if actual_size != expected_size:

            return name, False, (
                f"源文件大小不符({origin}): "
                f"期望 {expected_size}，实际 {actual_size}"
            )

        if expected_crc and actual_md5 != expected_crc:

            return name, False, (
                f"源文件 MD5 不符({origin}): "
                f"期望 {expected_crc}，实际 {actual_md5}"
            )

        try:

            client.upload_file(
                str(source),
                bucket,
                BUNDLE_KEY_PREFIX + name,
                ExtraArgs={"ContentType": "application/octet-stream"},
                Config=transfer_config,
            )

            head = client.head_object(
                Bucket=bucket,
                Key=BUNDLE_KEY_PREFIX + name,
            )

            if int(head.get("ContentLength", -1)) != expected_size:

                return name, False, (
                    "上传后大小仍不符: "
                    f"{head.get('ContentLength')} != {expected_size}"
                )

        except (BotoCoreError, ClientError, OSError) as exc:

            return name, False, f"{type(exc).__name__}: {exc}"

    return name, True, f"已用 {origin} 修复"


def main() -> int:

    if not MODIFIED_DIR.is_dir():

        raise RuntimeError("modified/ 不存在")

    catalog_path = find_catalog()

    import json

    with catalog_path.open("r", encoding="utf-8") as fh:
        catalog = json.load(fh)

    entries = catalog.get("BundleFiles")

    if not isinstance(entries, list):

        raise RuntimeError("bundleDownloadInfo.json 中不存在 BundleFiles")

    account_id = required_env("R2_ACCOUNT_ID")
    access_key_id = required_env("R2_ACCESS_KEY_ID")
    secret_access_key = required_env("R2_SECRET_ACCESS_KEY")
    bucket = required_env("R2_BUCKET")

    client = boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name="auto",
        config=Config(
            retries={"max_attempts": 3, "mode": "adaptive"},
            max_pool_connections=MAX_WORKERS,
        ),
    )

    from boto3.s3.transfer import TransferConfig

    transfer_config = TransferConfig(
        multipart_threshold=8 * 1024 * 1024,
        multipart_chunksize=8 * 1024 * 1024,
        max_concurrency=2,
        use_threads=True,
    )

    print(f"catalog: {catalog_path}")
    print(f"catalog 条目: {len(entries)}")
    print("扫描 R2 对象列表…")

    remote_sizes = list_remote_sizes(client, bucket)

    print(f"R2 上 {BUNDLE_KEY_PREFIX} 前缀下共 {len(remote_sizes)} 个对象")

    # --------------------------------------------------------
    # 比对：只要 R2 上「存在」这个 key，其大小就必须等于
    # catalog 声明的 Size。
    #
    # catalog 的 Crc/Size 是构建流程最终写定的权威值：
    #   * 被替换过的 bundle，calculateMD5 会把真实 MD5/Size 写回去
    #   * 没被替换的 bundle，就是官方原值
    # 因此任何 size 不符的对象都是脏数据，会让客户端校验失败、
    # 删掉重下、永远下不完。
    #
    # R2 上不存在该 key 的条目直接跳过 —— 客户端会回落到官方源。
    # --------------------------------------------------------

    bad: list[dict] = []

    for entry in entries:

        name = entry.get("Name")

        if not name:
            continue

        key = BUNDLE_KEY_PREFIX + name

        if key not in remote_sizes:
            continue

        if remote_sizes[key] != int(entry["Size"]):

            bad.append(entry)

            print(
                f"[MISMATCH] {name} "
                f"catalog={entry['Size']} r2={remote_sizes[key]}"
            )

    if not bad:

        print()
        print("对账通过：R2 上所有存在的对象都与 catalog 一致。")

        return 0

    print()
    print(f"发现 {len(bad)} 个不一致对象，开始修复…")

    session = make_session()

    failures: list[tuple[str, str]] = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:

        futures = [
            executor.submit(
                repair_one,
                client,
                bucket,
                session,
                entry,
                transfer_config,
            )
            for entry in bad
        ]

        for future in as_completed(futures):

            name, ok, detail = future.result()

            if ok:

                print(f"[REPAIRED] {name} : {detail}")

            else:

                failures.append((name, detail))

                print(
                    f"[REPAIR-FAILED] {name} : {detail}",
                    file=sys.stderr,
                )

    print()
    print(
        f"对账完成：修复 {len(bad) - len(failures)}，"
        f"失败 {len(failures)}"
    )

    if failures:

        for name, detail in failures:

            print(f"  {name} : {detail}", file=sys.stderr)

        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
