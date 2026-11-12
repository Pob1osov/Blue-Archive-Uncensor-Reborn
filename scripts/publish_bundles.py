#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
publish_bundles.py —— 把 ba_bake/out 里烘焙好的 bundle 发布到 public/

要点
----
客户端下载 bundle 后按 catalog 里的 Crc 校验。烘焙会改变 bundle 字节，
所以每发一个 bundle，必须同步改写 bundleDownloadInfo.json 里对应条目的
Crc 与 Size；同时必须更新 .hash —— 只改 json 不改 hash，客户端会用旧
hash 判定「清单没变」，继续拿旧 catalog 校验新 bundle，结果校验失败。

IsPrologue 决定客户端何时下载：prologue 包在启动早期就要，非 prologue
按资源版本走。这里保持官方原值不动。
"""

from __future__ import annotations

import hashlib
import json
import random
import shutil
from pathlib import Path


def md5_file(p: Path) -> str:
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def publish(pub: Path, out: Path, verbose: bool = True) -> int:
    cat_root = pub / "AssetBundles" / "Catalog"
    vers = sorted(p.name for p in cat_root.iterdir() if p.is_dir())
    if not vers:
        raise SystemExit("!! public 下没有 catalog 版次目录")
    ver = vers[-1]
    cat_dir = cat_root / ver / "Android"
    info = cat_dir / "bundleDownloadInfo.json"
    if not info.exists():
        raise SystemExit("!! 缺少 %s" % info)

    man = json.loads(info.read_text(encoding="utf-8"))
    entries = {e["Name"]: e for e in man["BundleFiles"]}

    android = pub / "AssetBundles" / "Android"
    android.mkdir(parents=True, exist_ok=True)

    published = 0
    for b in sorted(out.glob("*.bundle")):
        name = b.name
        shutil.copy2(b, android / name)
        crc = md5_file(b)
        size = b.stat().st_size

        e = entries.get(name)
        if e is None:
            if verbose:
                print("  [WARN] catalog 里没有 %s —— 客户端不会请求它" % name)
            continue
        old = (e.get("Crc"), e.get("Size"))
        e["Crc"] = crc
        e["Size"] = size
        if verbose:
            flag = "" if old == (crc, size) else "  (更新)"
            print("  %-58s %d B%s" % (name[:58], size, flag))
        published += 1

    info.write_text(json.dumps(man, ensure_ascii=False), encoding="utf-8")
    # hash 与 json 必须成对更新，否则客户端不认这份新清单
    (cat_dir / "bundleDownloadInfo.json.hash").write_text(
        str(random.SystemRandom().randrange(10**9, 10**10 - 1)), encoding="utf-8")
    (cat_dir / "bundleDownloadInfo.hash").write_text(
        str(random.SystemRandom().randrange(10**8, 10**9 - 1)), encoding="utf-8")

    if verbose:
        print("\n[publish] 发布 %d 个 bundle，catalog 版次 %s" % (published, ver))
    return published


if __name__ == "__main__":
    import sys
    ws = Path(__file__).resolve().parent
    pub = Path(r"C:\Users\POBLOSOV\.dsh\browser-sessions\ba-netlify\public")
    n = publish(pub, ws / "out")
    print("[publish] 完成，共 %d 个" % n)
