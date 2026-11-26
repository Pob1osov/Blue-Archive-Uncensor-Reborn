#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 MediaManifest 里的媒体对象全量镜像进 R2。

为什么需要这个
--------------
客户端启动/进关卡时会请求 `pool/MediaResources/<md5前两位>/<md5>`：
立绘、BGM、过场视频、场景背景都在这条链路上。

我们的桶一直是「覆盖层」——只放改动过的对象，其余靠客户端回落官方源。
但实测（2026-11-26）证实：**客户端遇到 404 不会回落，而是直接报
「网络连接失败」**，卡在进游戏那一步。

所以这条链路必须自己兜住。做法是把 MediaManifest 里列出的对象
按需（R2 没有、或大小不符）从官方 CDN 拉下来再传进桶。

特性
----
* 可断点续跑：已存在且大小一致的对象直接跳过，跑多少次都不会重复下载。
* 单文件失败只计数，不中断整体；最后汇总，失败超过阈值才以非零码退出。
* 下载后校验 md5（文件名就是 md5），不对就重试，绝不上传坏对象。
* 上传后 head_object 校验 ContentLength。

用法
----
    python scripts/mirrorMedia.py                 # 全量补齐
    python scripts/mirrorMedia.py --limit 200     # 只做前 200 个（试跑）
    python scripts/mirrorMedia.py --dry-run       # 只统计缺口，不下载
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import boto3
import requests
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

ROOT_DIR = Path(__file__).resolve().parent.parent
MODIFIED_DIR = ROOT_DIR / "modified"

OFFICIAL_BASE = "https://static.bluearchive-cn.com/prodm39"
MEDIA_PREFIX = "pool/MediaResources"

MAX_WORKERS = 12
MAX_ATTEMPTS = 3
RETRY_BASE_SECONDS = 2

_thread_local = threading.local()
_lock = threading.Lock()
_done = 0
_skipped = 0
_uploaded = 0
_failed: list[str] = []


def log(message: str) -> None:
    print(message, flush=True)


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"缺少环境变量: {name}")
    return value.strip()


def get_client():
    client = getattr(_thread_local, "client", None)
    if client is None:
        account_id = required_env("R2_ACCOUNT_ID")
        client = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=required_env("R2_ACCESS_KEY_ID"),
            aws_secret_access_key=required_env("R2_SECRET_ACCESS_KEY"),
            region_name="auto",
            config=Config(
                max_pool_connections=MAX_WORKERS * 2,
                retries={"max_attempts": 3, "mode": "standard"},
                signature_version="s3v4",
            ),
        )
        _thread_local.client = client
    return client


def find_manifest(explicit: str | None) -> Path:
    """优先用本次构建产出的 MediaManifest（与客户端要的版本一致）。"""
    if explicit:
        path = Path(explicit)
        if not path.is_absolute():
            path = ROOT_DIR / path
        if not path.is_file():
            raise SystemExit(f"找不到 MediaManifest: {path}")
        return path

    base = MODIFIED_DIR / "Manifest" / "MediaResources"
    candidates = sorted(base.glob("*/MediaManifest"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        raise SystemExit(
            f"找不到 {base}/*/MediaManifest；请先跑 scripts/fetchMediaManifest.py"
        )
    return candidates[-1]


def parse_manifest(path: Path) -> list[tuple[str, int]]:
    """MediaManifest 是 CSV：name,md5,type,size,（每行一条）。"""
    entries: list[tuple[str, int]] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) < 4:
            continue
        md5 = parts[1].strip()
        size_text = parts[3].strip()
        if len(md5) != 32 or not size_text.isdigit():
            continue
        entries.append((md5, int(size_text)))
    return entries


def key_for(md5: str) -> str:
    return f"{MEDIA_PREFIX}/{md5[:2]}/{md5}"


def remote_size(client, bucket: str, key: str) -> int | None:
    try:
        head = client.head_object(Bucket=bucket, Key=key)
        return int(head.get("ContentLength", -1))
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def download_official(md5: str) -> bytes:
    url = f"{OFFICIAL_BASE}/{key_for(md5)}"
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            resp = requests.get(url, timeout=180)
            if resp.status_code == 404:
                raise RuntimeError(f"官方也没有这个对象: {url}")
            resp.raise_for_status()
            data = resp.content
            actual = hashlib.md5(data).hexdigest()
            if actual != md5:
                raise RuntimeError(f"md5 不符 期望 {md5} 实得 {actual} ({len(data)} B)")
            return data
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt + 1 < MAX_ATTEMPTS:
                time.sleep(RETRY_BASE_SECONDS * (2**attempt))
    raise RuntimeError(f"下载失败: {url} ({last})")


def mirror_one(md5: str, size: int, bucket: str) -> str:
    """返回 'skipped' / 'uploaded' / 'failed'。"""
    key = key_for(md5)
    client = get_client()

    for attempt in range(MAX_ATTEMPTS):
        try:
            current = remote_size(client, bucket, key)
            if current == size:
                return "skipped"
            data = download_official(md5)
            if len(data) != size:
                raise RuntimeError(f"大小不符 期望 {size} 实得 {len(data)}")
            client.put_object(
                Bucket=bucket,
                Key=key,
                Body=data,
                ContentLength=len(data),
                ContentType="application/octet-stream",
            )
            check = remote_size(client, bucket, key)
            if check != size:
                raise RuntimeError(f"上传后校验失败 期望 {size} 实得 {check}")
            return "uploaded"
        except Exception as exc:  # noqa: BLE001
            if attempt + 1 >= MAX_ATTEMPTS:
                with _lock:
                    _failed.append(f"{key}: {exc}")
                return "failed"
            time.sleep(RETRY_BASE_SECONDS * (2**attempt))
    return "failed"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default=None, help="MediaManifest 路径")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个（0=全部）")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS)
    parser.add_argument("--dry-run", action="store_true", help="只统计缺口")
    parser.add_argument(
        "--max-failures",
        type=int,
        default=200,
        help="失败超过这个数就以非零码退出（默认 200）",
    )
    args = parser.parse_args()

    manifest = find_manifest(args.manifest)
    entries = parse_manifest(manifest)
    log(
        f"[MEDIA] 清单 {manifest.relative_to(ROOT_DIR)}：{len(entries)} 个对象 / "
        f"{sum(s for _, s in entries) / 1048576:.1f} MB"
    )

    if args.dry_run:
        bucket = required_env("R2_BUCKET")
        missing = 0
        missing_bytes = 0
        for md5, size in entries:
            try:
                current = remote_size(get_client(), bucket, key_for(md5))
            except Exception as exc:  # noqa: BLE001
                log(f"[WARN] 读取 {md5} 失败: {exc}")
                continue
            if current != size:
                missing += 1
                missing_bytes += size
        log(f"[DRY-RUN] 缺口 {missing} 个 / {missing_bytes / 1048576:.1f} MB")
        return 0

    bucket = required_env("R2_BUCKET")
    targets = entries[: args.limit] if args.limit > 0 else entries
    log(f"[MEDIA] 开始镜像 {len(targets)} 个，并发 {args.workers}")

    total = len(targets)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(mirror_one, md5, size, bucket): md5 for md5, size in targets
        }
        for index, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            global _done, _skipped, _uploaded
            with _lock:
                _done = index
                if result == "skipped":
                    _skipped += 1
                elif result == "uploaded":
                    _uploaded += 1
            if index % 500 == 0 or index == total:
                log(
                    f"[MEDIA] {index}/{total}  已上传 {_uploaded}  跳过 {_skipped}  "
                    f"失败 {len(_failed)}"
                )

    log(
        f"[MEDIA] 完成：上传 {_uploaded} / 跳过 {_skipped} / 失败 {len(_failed)}"
    )
    for item in _failed[:20]:
        log(f"[FAIL] {item}")
    if len(_failed) > args.max_failures:
        log(f"[MEDIA] 失败 {len(_failed)} 个，超过阈值 {args.max_failures}，中止")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, BotoCoreError) as exc:  # noqa: BLE001
        log(f"[ERROR] {exc}")
        sys.exit(1)
