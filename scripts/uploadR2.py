from __future__ import annotations

import mimetypes
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
)


ROOT_DIR = Path(__file__).resolve().parent.parent
MODIFIED_DIR = ROOT_DIR / "modified"

# ------------------------------------------------------------
# 并发与重试
# ------------------------------------------------------------
#
# 原来这里是 max_concurrency=8 + ThreadPoolExecutor(max_workers=4)，
# 并且完全没有重试、没有上传后校验。
#
# 实测发现 R2 上出现了「对象存在但 Content-Length / ETag / Last-Modified
# 全部缺失，且下载回来的字节数与 catalog 不符」的损坏对象
# （prolog 相关 3 个 bundle）。表现是客户端每次启动都重新下载、
# 校验失败、永远下不完，网络下行常年 13~15 MB/s。
#
# 全量镜像有 2 万多个文件、十几个 GB，高并发下撞上网络抖动或限流就会
# 写坏对象。因此：
#   * 降低并发，减少瞬时压死
#   * 每个文件失败后指数退避重试
#   * 上传后 head_object 校验 ContentLength，不符就重传
#   * 单个文件彻底失败只记录，不中断其它文件；最后汇总并以非零码退出
# ------------------------------------------------------------

MAX_WORKERS = 2
MAX_CONCURRENCY = 4
MAX_ATTEMPTS = 4
RETRY_BASE_SECONDS = 2


def required_env(name: str) -> str:
    value = os.environ.get(name)

    if not value:
        raise RuntimeError(
            f"缺少环境变量: {name}"
        )

    return value.strip()


def content_type_for(
    local_path: Path,
    guess: str | None,
) -> str:
    if (
        local_path.name.endswith(".hash")
        or local_path.name == "TableManifestHash"
    ):
        return "text/plain"

    if local_path.suffix.lower() == ".json":
        return "application/json"

    return guess or "application/octet-stream"


def verify_object(
    client,
    bucket: str,
    key: str,
    expected_size: int,
) -> tuple[bool, str]:
    """确认对象真的写进去了，且大小与本地一致。"""

    try:

        head = client.head_object(
            Bucket=bucket,
            Key=key,
        )

    except ClientError as exc:

        return False, (
            f"head_object 失败: "
            f"{exc.response.get('Error', {}).get('Code', exc)}"
        )

    actual = head.get("ContentLength")

    if actual is None:

        return False, "对象缺少 ContentLength"

    if int(actual) != int(expected_size):

        return False, (
            f"大小不一致: 期望 {expected_size}，"
            f"R2 实际 {actual}"
        )

    return True, ""


def upload_file(
    client,
    bucket: str,
    local_path: Path,
    key: str,
    config,
) -> tuple[bool, str]:

    local_size = local_path.stat().st_size

    ct = content_type_for(
        local_path,
        mimetypes.guess_type(str(local_path))[0],
    )

    last_error = ""

    for attempt in range(1, MAX_ATTEMPTS + 1):

        try:

            client.upload_file(
                str(local_path),
                bucket,
                key,
                ExtraArgs={
                    "ContentType": ct,
                },
                Config=config,
            )

            ok, why = verify_object(
                client,
                bucket,
                key,
                local_size,
            )

            if ok:
                return True, ""

            last_error = why

            print(
                f"  [RETRY {attempt}/{MAX_ATTEMPTS}] "
                f"{key} 上传后校验失败: {why}"
            )

        except (BotoCoreError, ClientError, OSError) as exc:

            last_error = f"{type(exc).__name__}: {exc}"

            print(
                f"  [RETRY {attempt}/{MAX_ATTEMPTS}] "
                f"{key} 上传异常: {last_error}"
            )

        if attempt < MAX_ATTEMPTS:

            wait = min(
                RETRY_BASE_SECONDS ** attempt,
                30,
            )

            print(f"  {wait}s 后重试…")

            time.sleep(wait)

    return False, last_error


def main() -> int:

    if not MODIFIED_DIR.is_dir():

        raise RuntimeError(
            "modified/ 不存在"
        )

    account_id = required_env("R2_ACCOUNT_ID")

    access_key_id = required_env(
        "R2_ACCESS_KEY_ID"
    )

    secret_access_key = required_env(
        "R2_SECRET_ACCESS_KEY"
    )

    bucket = required_env("R2_BUCKET")

    endpoint = (
        f"https://{account_id}"
        ".r2.cloudflarestorage.com"
    )

    # 客户端层重试 + 限流容忍，与上面的显式重试叠加，
    # 覆盖一次请求内的短暂抖动
    boto_config = Config(
        retries={
            "max_attempts": 3,
            "mode": "adaptive",
        },
        max_pool_connections=MAX_CONCURRENCY,
    )

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name="auto",
        config=boto_config,
    )

    from boto3.s3.transfer import TransferConfig

    config = TransferConfig(
        multipart_threshold=8 * 1024 * 1024,
        multipart_chunksize=8 * 1024 * 1024,
        max_concurrency=MAX_CONCURRENCY,
        use_threads=True,
    )

    files = sorted(
        path
        for path in MODIFIED_DIR.rglob("*")
        if path.is_file()
    )

    print(
        f"准备上传 {len(files)} 个文件"
        f"（并发 {MAX_WORKERS}，"
        f"单片并发 {MAX_CONCURRENCY}，"
        f"最多重试 {MAX_ATTEMPTS} 次）"
    )

    failures: list[tuple[str, str]] = []
    done = 0

    def job(local_path: Path) -> tuple[str, bool, str]:

        relative = local_path.relative_to(
            MODIFIED_DIR
        )

        key = relative.as_posix()

        ok, why = upload_file(
            client,
            bucket,
            local_path,
            key,
            config,
        )

        return key, ok, why

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = [
            executor.submit(job, path)
            for path in files
        ]

        for future in as_completed(futures):

            key, ok, why = future.result()

            done += 1

            if ok:

                print(f"[UPLOADED] {key}")

            else:

                failures.append((key, why))

                print(
                    f"[FAILED] {key} : {why}",
                    file=sys.stderr,
                )

            if done % 500 == 0:

                print(
                    f"[PROGRESS] {done}/{len(files)} "
                    f"失败 {len(failures)}"
                )

    print()
    print(
        f"R2 上传完成：成功 {len(files) - len(failures)}，"
        f"失败 {len(failures)}"
    )

    if failures:

        print()
        print("以下文件上传失败：", file=sys.stderr)

        for key, why in failures:

            print(
                f"  {key} : {why}",
                file=sys.stderr,
            )

        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
