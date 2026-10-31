from __future__ import annotations

import argparse
import json
import os
import re

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def create_session(total_retries: int = 5) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=total_retries,
        connect=5,
        read=5,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def get_official_versions() -> dict[str, str]:
    """直连官方 API 拿版本号。代理不可用时走这条路径。"""

    # 官方 API 本身比较稳，给正常重试
    session = create_session(total_retries=5)

    print("[INFO] 尝试直连官方 API...")
    r = session.get("https://bluearchive-cn.com/api/meta/setup", timeout=30)
    r.raise_for_status()
    match = re.search(r"(\d+\.\d+\.\d+)", r.text)
    if not match:
        raise RuntimeError("无法从 setup API 中找到 GameVersion")
    game_version = match.group(1)

    r = session.get(
        "https://gs-api.bluearchive-cn.com/api/state",
        headers={
            "APP-VER": game_version,
            "PLATFORM-ID": "1",
            "CHANNEL-ID": "2",
        },
        timeout=30,
    )
    r.raise_for_status()
    info = r.json()

    return {
        "GameVersion": str(info.get("GameVersion", game_version)),
        "ResourceVersion": str(info["ResourceVersion"]),
        "TableVersion": str(info["TableVersion"]),
        "MediaVersion": str(info.get("MediaVersion", "")),
    }


def get_current_versions() -> dict[str, str]:
    """优先用代理；代理不可用时快速降级到官方 API。"""

    # 代理指向的是一个自建 Worker，随时可能下线。
    # 对一个「连接被拒绝」的地址做 5 次指数退避重试（1+2+4+8+16 秒，
    # 叠加 30 秒超时）会白等十几分钟，把整个 job 卡死在 Check version。
    # 所以代理路径只做一次快速尝试（短超时 + 0 重试），失败立刻降级。

    proxy_url = os.environ.get("VERSION_API_URL")

    if proxy_url:

        print(f"[INFO] 使用代理 API: {proxy_url}")

        try:

            quick = create_session(total_retries=0)

            r = quick.get(proxy_url, timeout=(3, 5))
            r.raise_for_status()
            info = r.json()

            return {
                "GameVersion": str(info.get("GameVersion", "1.0.0")),
                "ResourceVersion": str(info["ResourceVersion"]),
                "TableVersion": str(info["TableVersion"]),
                "MediaVersion": str(info.get("MediaVersion", "")),
            }

        except Exception as exc:

            print(
                f"[WARN] 代理不可用（{type(exc).__name__}: {exc}），"
                "改为直连官方 API"
            )

    return get_official_versions()


def write_github_output(values: dict[str, str]) -> None:
    output_file = os.environ.get("GITHUB_OUTPUT")
    if not output_file:
        return
    with open(output_file, "a", encoding="utf-8") as f:
        f.write(f"game_version={values['GameVersion']}\n")
        f.write(f"resource_version={values['ResourceVersion']}\n")
        f.write(f"table_version={values['TableVersion']}\n")
        f.write(f"media_version={values['MediaVersion']}\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-output", type=str, default=None)
    parser.add_argument("--github-output", action="store_true")
    args = parser.parse_args()

    values = get_current_versions()

    print(f"GameVersion: {values['GameVersion']}")
    print(f"ResourceVersion: {values['ResourceVersion']}")
    print(f"TableVersion: {values['TableVersion']}")

    if args.json_output:
        with open(args.json_output, "w", encoding="utf-8") as f:
            json.dump(values, f, ensure_ascii=False, indent=2)

    if args.github_output:
        write_github_output(values)


if __name__ == "__main__":
    main()
