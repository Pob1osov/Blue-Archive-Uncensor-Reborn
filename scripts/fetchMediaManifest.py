#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把官方的 MediaManifest / MediaManifestHash 原样放进 modified/。

背景
----
update_resources.py 只产出三类东西：
    AssetBundles/Android/*.bundle
    AssetBundles/Catalog/<ResourceVersion>/Android/bundleDownloadInfo.json|hash
    Manifest/TableBundles/<TableVersion>/TableManifest|TableManifestHash
    pool/TableBundles/<xx>/<crc>

但客户端启动期除了表清单，还会请求：

    /Manifest/MediaResources/<MediaVersion>/MediaManifest
    /Manifest/MediaResources/<MediaVersion>/MediaManifestHash

这两个文件属于官方发行包，流水线从来不生成。以前把 modified/ 直接推到
Netlify 时它们会整份消失，于是媒体资源（立绘、BGM、过场动画）全部 404，
表现是进游戏后黑屏或长时间卡在加载界面。

所以部署前必须把它们补回来。这里不重写内容，只做转发：直接从官方 CDN
取同一个文件，字节级原样保留——一旦改动，客户端会认为清单被篡改。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

ROOT_DIR = Path(__file__).resolve().parent.parent
MODIFIED_DIR = ROOT_DIR / "modified"

OFFICIAL_MEDIA_MANIFEST_BASE = (
    "https://static.bluearchive-cn.com"
    "/prodm39/Manifest/MediaResources"
)

MEDIA_FILES = ("MediaManifest", "MediaManifestHash")


def log(msg: str) -> None:
    print(msg, flush=True)


def fetch(url: str, timeout: int = 120) -> bytes:
    # 官方 CDN 偶发 5xx，重试两次再放弃
    last: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=timeout)
            r.raise_for_status()
            return r.content
        except Exception as exc:  # noqa: BLE001
            last = exc
            log(f"[WARN] 第 {attempt + 1} 次下载失败 {url}: {exc}")
    raise RuntimeError(f"下载失败: {url} ({last})")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--current-version-file",
        default="current_version.json",
        help="getCurrentVersion.py 产出的版本信息 JSON",
    )
    args = parser.parse_args()

    version_file = Path(args.current_version_file)
    if not version_file.is_absolute():
        version_file = ROOT_DIR / version_file
    if not version_file.is_file():
        raise SystemExit(f"找不到版本文件: {version_file}")

    info = json.loads(version_file.read_text(encoding="utf-8"))
    media_version = str(info.get("MediaVersion") or "").strip()
    if not media_version:
        raise SystemExit("版本文件里没有 MediaVersion，无法定位 MediaManifest")

    dest_dir = (
        MODIFIED_DIR / "Manifest" / "MediaResources" / media_version
    )
    dest_dir.mkdir(parents=True, exist_ok=True)

    base = f"{OFFICIAL_MEDIA_MANIFEST_BASE}/{media_version}"
    for name in MEDIA_FILES:
        data = fetch(f"{base}/{name}")
        target = dest_dir / name
        target.write_bytes(data)
        log(f"[INFO] MediaManifest 已就位: {target.relative_to(ROOT_DIR)}  {len(data)} B")

    log(f"[INFO] MediaVersion = {media_version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
