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

# 旧版 ExcelDB：上游 InfiniteAstra 放在自己的镜像上，供「按 ID 整行还原」用。
# 2026-11-17 决定重新启用该机制（与上游 dev-r2 的 updateExcelDB.py 对齐），
# 但做了两处加固：
#   · 官方表增删行导致对不齐时，跳过该行而不是静默失败
#   · 还原会删掉某些词（桃信 -> 空），因此还原后再由 applyTextPatch 补回日服词
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
    *args: str,
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
            *args,
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


# ============================================================
# 文本层变更检测
# ============================================================

TEXT_PATCH_STAMP = ROOT_DIR / ".textpatch_stamp"


def _text_patch_fingerprint(official_excel_crc: str) -> str:
    """文本改写指纹 = applyTextPatch.py 内容 MD5 + 官方 ExcelDB Crc。

    只有两者之一变化时才需要重写 ExcelDB：
      * 脚本变了   -> 替换规则可能变
      * 官方表变了 -> 必须在新表上重新应用

    都没变时，重写结果必然与上次一致，跳过即可，
    客户端也就不会产生 160MB 的下载。
    """
    import hashlib

    script = SCRIPTS_DIR / "applyTextPatch.py"
    h = hashlib.md5()
    h.update(script.read_bytes() if script.is_file() else b"missing")
    h.update(b"|")
    h.update(str(official_excel_crc).encode())
    return h.hexdigest()


def _read_stamp() -> str:
    try:
        return TEXT_PATCH_STAMP.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _write_stamp(value: str) -> None:
    try:
        TEXT_PATCH_STAMP.write_text(value + "\n", encoding="utf-8")
    except Exception as exc:
        print(f"[WARN] 写 stamp 失败: {exc}")


def load_exclusion_baseline() -> dict[str, dict]:
    """读取 assetexclusions/.baseline.json。

    结构示例：
        {
          "ui-_mxload-prefabs-2026-06-04_assets_all_2022518488.bundle": {
            "based_on": "14a7964e7a50",
            "note": "MomoTalk 标题宽度 314->448，问号右移"
          }
        }

    based_on 记录「改动基于官方哪个版本的 Crc 前 12 位」，
    官方换版时能提示改动可能需要重新适配。
    """

    path = EXCLUSIONS_DIR / ".baseline.json"

    if not path.is_file():
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[WARN] 解析 .baseline.json 失败，按空处理: {e}")
        return {}

    if not isinstance(data, dict):
        return {}

    return {k: v for k, v in data.items() if isinstance(v, dict)}


def rebake_replacement_into_exclusions(
    exclusions: dict[str, Path],
    catalog_entries: dict[str, dict],
) -> int:
    """把 replacement/ 里的 PNG 烘焙进已复制到 modified/ 的 exclusion bundle。

    背景
    ----
    replaceTexture2D.py 把 assetexclusions/ 排除在输入之外，
    而 copy_exclusion_bundles 在烘焙之后整包覆盖 modified/，
    导致 exclusions 包里与 replacement 同名的贴图会把烘焙结果盖回去。

    典型场景：exclusions 里的 imagefont 包（来自第三方）中 ImgFont_Momotalk
    是官方的中文版，而 replacement/ 里有我们的英文版 —— 直接推上去就是中文。

    做法
    ----
    对每个 exclusion bundle：
      1. 在 replacement/ 下找同名目录（目录名 = bundle 文件名去掉后缀）
      2. 没有该目录就跳过（这个包不带贴图替换）
      3. 有则用 UnityPy 就地替换对应 Texture2D 并回写 modified/ 里的那份

    这样「第三方的整包修改」与「我们自己的贴图替换」就能叠加生效。
    """

    import UnityPy  # 延迟导入：没有该依赖时不影响其它流程

    destination_dir = MODIFIED_DIR / "AssetBundles" / "Android"

    replacement_dir = ROOT_DIR / "replacement"

    if not replacement_dir.is_dir():
        return 0

    # 建立 replacement 子目录索引：规范化后的 bundle 名 -> 目录
    rep_index: dict[str, Path] = {}
    for d in replacement_dir.iterdir():
        if d.is_dir():
            rep_index[d.name.strip().lower()] = d

    rebaked = 0

    for name in exclusions:

        target = destination_dir / name
        if not target.is_file():
            continue

        # 找 replacement 里对应的目录
        #
        # bundle 名形如：
        #   prologgroup-...-imagefont-_mxprolog-2026-06-04_assets_all_2403408782.bundle
        # replacement 目录是其前缀：
        #   prologgroup-...-imagefont-_mxprolog
        #
        # 因此按「目录名是 bundle 名的前缀」匹配，取最长的那个，
        # 避免同名包之间误配。
        stem = name
        if stem.endswith(".bundle"):
            stem = stem[: -len(".bundle")]
        stem_low = stem.strip().lower()

        rep_dir = None
        best_len = 0
        for dir_key, dir_path in rep_index.items():
            if stem_low.startswith(dir_key) and len(dir_key) > best_len:
                rep_dir = dir_path
                best_len = len(dir_key)

        if rep_dir is None:
            continue

        pngs = {p.stem: p for p in rep_dir.glob("*.png")}
        if not pngs:
            continue

        try:
            env = UnityPy.load(str(target))
            hit = 0
            for obj in env.objects:
                if obj.type.name != "Texture2D":
                    continue
                try:
                    data = obj.read()
                    tex_name = data.m_Name
                except Exception:
                    continue
                if tex_name not in pngs:
                    continue
                from PIL import Image as _Image
                data.image = _Image.open(pngs[tex_name]).convert("RGBA")
                data.save()
                hit += 1

            if hit:
                tmp_dir = MODIFIED_DIR / "_rebake_tmp"
                tmp_dir.mkdir(parents=True, exist_ok=True)
                import os
                cwd = os.getcwd()
                os.chdir(str(MODIFIED_DIR))
                try:
                    # pack="original" 保持原来的压缩方式。
                    # 默认 pack="none" 会导致包体积膨胀 3 倍以上，
                    # 客户端解压耗时剧增、卡在「正在初始化游戏资源」。
                    env.save(pack="original", out_path=str(tmp_dir))
                finally:
                    os.chdir(cwd)

                produced = tmp_dir / name
                if produced.is_file():
                    shutil.move(str(produced), str(target))
                    print(f"[REBAKE-EXCLUSION] {name} —— 叠加 {hit} 张替换图")
                    rebaked += 1
                shutil.rmtree(tmp_dir, ignore_errors=True)

        except Exception as exc:
            print(f"[WARN-REBAKE] {name} 叠加失败，保持原样: {exc}")

    if rebaked:
        print(f"[REBAKE-EXCLUSION] 共 {rebaked} 个 exclusion 包叠加了替换图")

    return rebaked


def copy_exclusion_bundles(
    exclusions: dict[str, Path],
    catalog_entries: dict[str, dict],
) -> int:
    """把 assetexclusions 复制进 modified/，并区分「过期副本」与「有意修改」。

    为什么需要校验
    --------------
    assetexclusions 有两个用途，必须分开对待：

    (A) 修坏对象：R2 上某个 bundle 传坏了，这里放一份已知正确的副本。
        这种情况本地文件应与官方 Catalog 的 Size/Crc **完全一致**。

    (B) 有意修改：我们自己改过这个 bundle（去和谐贴图、改 UI 布局等）。
        这种情况本地文件与官方**必然不同**，而且是我们要的效果。

    历史上只有 (A) 的校验、没有 (B) 的概念，出过一次事故：
    官方 2026-11-05 更新资源后，43 个 spine 立绘包的本地旧副本
    （比官方大 2~7 倍）被无差别复制进 modified/ 并推上 R2，
    客户端拉到后 spine 立绘加载失败 —— 表现为**进游戏黑屏**。

    如何区分
    --------
    在 assetexclusions/.baseline.json 里记录「每个包是基于哪个官方版本改的」。
    文件里登记过的，视为有意修改，放行；
    没登记的，必须与官方一致，否则判定为过期副本，跳过。
    """

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

    baseline = load_exclusion_baseline()

    copied = 0
    skipped = 0

    for name, source in exclusions.items():

        target = destination_dir / name

        actual_size = source.stat().st_size
        actual_md5 = hashlib.md5(source.read_bytes()).hexdigest()

        # ---- 情况 B：已在 baseline 登记为「有意修改」，直接放行 ----
        if name in baseline:

            entry = catalog_entries.get(name)

            if entry is None:
                print(
                    f"[SKIP-EXCLUSION] {name} —— 官方 Catalog 中已无此 Bundle，"
                    f"无法确认改动是否仍有效，跳过"
                )
                skipped += 1
                continue

            base_ver = baseline[name].get("based_on", "")
            cur_ver = str(entry.get("Crc", ""))[:12]

            if base_ver and cur_ver and base_ver != cur_ver:
                # 官方换过这个包，我们的改动可能已失效
                print(
                    f"[WARN-EXCLUSION] {name} —— 官方已更新此 Bundle\n"
                    f"    baseline 基于: {base_ver}\n"
                    f"    当前官方    : {cur_ver}\n"
                    f"    仍按有意修改放行（如需重新适配请重新打包）"
                )

            shutil.copy2(source, target)
            print(
                f"[COPY-EXCLUSION] {name} —— 有意修改"
                f"（{actual_size} B / {actual_md5[:12]}）"
            )
            copied += 1
            continue

        # ---- 情况 A：未登记，必须与官方一致 ----
        entry = catalog_entries.get(name)

        if entry is None:
            print(f"[SKIP-EXCLUSION] {name} —— 官方 Catalog 中已无此 Bundle，跳过")
            skipped += 1
            continue

        expected_size = int(entry["Size"])
        expected_crc = str(entry.get("Crc", "")).lower()

        if actual_size != expected_size or (expected_crc and actual_md5 != expected_crc):
            print(
                f"[SKIP-EXCLUSION] {name} —— 本地副本已过期，跳过\n"
                f"    官方: {expected_size} B / {expected_crc}\n"
                f"    本地: {actual_size} B / {actual_md5}"
            )
            skipped += 1
            continue

        shutil.copy2(source, target)

        print(f"[COPY-EXCLUSION] {source} -> {target}")

        copied += 1

    if skipped:
        print(f"[COPY-EXCLUSION] 跳过 {skipped} 个过期副本（客户端将回落到官方源）")

    return copied


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

    # 工作流里勾选 force 时会传 --force 进来，
    # 忽略 current.txt 与官方版本的比对，强制执行一次完整构建。
    parser.add_argument(
        "--force",
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

    if args.force:

        print()
        print(
            "[FORCE] 忽略版本比对，强制执行完整构建。"
        )

        changed = True

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
    # 这一步必须无条件执行。
    #
    # 原来的写法是 if downloaded_count > 0 才跑，理由是「官方只更新 TableVersion
    # 时 differences 为 0，根目录没有 .bundle，脚本会以退出码 1 中断流水线」。
    # 但那个「保护」本身才是真正的 bug：一旦真的没有新 .bundle，
    # replacement/ 里的替换图就永远不会被烘焙，R2 上的 UI 便一直是官方中文，
    # 而且整个流程一切正常、毫无报错 —— 视觉层就这样静默失效了。
    #
    # 现在 replaceTexture2D.py 已支持「根目录没有就按官方 Catalog 现下」，
    # 因此无论官方这次动没动 AssetBundle，都能把 96 张替换图烤进对应 bundle。
    # ========================================================

    run_script(
        "replaceTexture2D.py"
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

    # ========================================================
    # 5.1 部署 Unity Addressables 目录文件
    # ========================================================
    #
    # 客户端启动时卡在「Initialize Catalog」，实测 CF 日志确认它请求的是：
    #     {ResUrls}/AssetBundles/Catalog/<ResourceVersion>/Android/catalog_Android.zip
    #     {ResUrls}/AssetBundles/Catalog/<ResourceVersion>/Android/catalog_Android.hash
    #
    # 官方 CDN 上 catalog_Android.json 是 404，只有 .zip（11.7 MB，内含
    # catalog_Remote.json 77 MB）和 .hash（32 字节），所以必须按同样格式提供。
    #
    # 之前的教训：往根目录放 catalog_Remote.json 没用，因为客户端请求的
    # 路径带版本号、且要的是 zip。另外那份 json 必须是**官方当前版本**，
    # 旧版本哪怕只差 6 字节也会被拒绝。
    #
    # 仓库里存 gz 版（GitHub 单文件限 25MB），这里还原成 zip/hash 放对位置。
    # ========================================================

    _catalog_dir = (
        MODIFIED_DIR
        / "AssetBundles"
        / "Catalog"
        / official_resource_version
        / "Android"
    )
    _catalog_dir.mkdir(parents=True, exist_ok=True)

    _copied_catalog = 0

    # 1) zip 放在两处，覆盖客户端可能请求的所有路径
    #
    # 实测设备上游戏会生成 catalog_Remote.zip（下载中的临时文件），
    # 说明它请求的可能是根目录的 catalog_Remote.zip；
    # 而 CF 日志里又出现过 AssetBundles/Catalog/<ver>/Android/catalog_Android.zip。
    # 两个都放，避免再猜错。
    for _zn in ("catalog_Android.zip", "catalog_Remote.zip"):
        _z = ROOT_DIR / _zn
        if not _z.is_file():
            continue
        # 放到版本目录
        shutil.copy2(_z, _catalog_dir / _zn)
        print(
            f"[CATALOG] {_zn} -> {_catalog_dir.relative_to(MODIFIED_DIR)}"
            f"  ({_z.stat().st_size} B)"
        )
        _copied_catalog += 1

    # 根目录也放一份 catalog_Remote.zip（约 12 MB，客户端临时文件同名）
    _zr = ROOT_DIR / "catalog_Remote.zip"
    if _zr.is_file():
        shutil.copy2(_zr, MODIFIED_DIR / "catalog_Remote.zip")
        print(f"[CATALOG] catalog_Remote.zip -> 根目录 ({_zr.stat().st_size} B)")

    # 2) 两个 hash（内容相同，都是官方当前的 catalog hash）
    for _hn in ("catalog_Android.hash", "catalog_Remote.hash"):
        _hs = ROOT_DIR / _hn
        if _hs.is_file():
            shutil.copy2(_hs, _catalog_dir / _hn)
            print(f"[CATALOG] {_hn} -> {_catalog_dir.relative_to(MODIFIED_DIR)}")
            _copied_catalog += 1

    # 3) 兼容：根目录也放一份 catalog_Remote.json（部分版本会直接请求）
    _gzk = ROOT_DIR / "catalog_Remote.json.gz"
    if _gzk.is_file():
        import gzip
        with gzip.open(_gzk, "rb") as _f:
            _data = _f.read()
        (MODIFIED_DIR / "catalog_Remote.json").write_bytes(_data)
        print(f"[CATALOG] catalog_Remote.json 解压到根目录 ({len(_data)} B)")
        _copied_catalog += 1

    if not _copied_catalog:
        print("[WARN] 仓库里没有 catalog 文件，客户端可能无法初始化")

    # ========================================================
    # 5.2 部署 Patch 热更文件
    # ========================================================
    #
    # 客户端启动时会读 Patch/patch_assetmanifest，里面记录：
    #     3_0_2,<md5>
    #     3_0_3,<md5>
    # 然后按 md5 去 {ResUrls}/pool/Patch/<md5> 下载。
    #
    # 我们的源上缺这个文件时，游戏会停在「需要下载 16.43 MB 的更新包，
    # 是否继续？」，点确认也没反应（因为 404）。
    #
    # 官方地址实测：static.bluearchive-cn.com/prodm39/pool/Patch/<md5>
    # 大小 67,308 B，MD5 与文件名一致。
    # ========================================================

    _patch_src = ROOT_DIR / "patch_3_0_2.bytes"
    if _patch_src.is_file():
        import hashlib as _hl
        _pd = _patch_src.read_bytes()
        _ph = _hl.md5(_pd).hexdigest()
        _pdir = MODIFIED_DIR / "pool" / "Patch" / _ph[:2]
        _pdir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_patch_src, _pdir / _ph)
        print(f"[PATCH] pool/Patch/{_ph[:2]}/{_ph}  ({len(_pd)} B)")
    else:
        print("[WARN] 仓库里没有 patch_3_0_2.bytes，客户端可能卡在更新提示")

    exclusion_count = copy_exclusion_bundles(
        exclusions,
        build_bundle_map(official_catalog),
    )

    # ========================================================
    # 5.5 把 replacement/ 的贴图再烘焙进 exclusions 里的包
    # ========================================================
    #
    # 为什么需要这一步
    # ----------------
    # replaceTexture2D.py 明确把 assetexclusions/ 排除在输入之外
    # （见它 main() 里的 excluded_dirs），因此 exclusions 里的 bundle
    # 从不参与烘焙；而 copy_exclusion_bundles 是在烘焙**之后**整包覆盖，
    # 于是：
    #
    #   replacement/ 有英文 MomoTalk.png  -> 烘焙进 modified/ 的包
    #   exclusions/  有中文 MomoTalk 的包 -> 整包覆盖回去
    #   结果：界面变回中文，烘焙白做
    #
    # 实测过一次：UP 主提供的 imagefont bundle 里 ImgFont_Momotalk 与官方
    # 完全相同（中文「桃信」），推上去后 MomoTalk 界面回退成中文。
    #
    # 修法：对每个已复制的 exclusion bundle，如果 replacement/ 里存在
    # 同名 bundle 目录下的 PNG，就地烘焙一次，让两者叠加而不是互相覆盖。
    # ========================================================

    rebaked = rebake_replacement_into_exclusions(
        exclusions,
        build_bundle_map(official_catalog),
    )

    # ========================================================
    # 5.6 rebake 之后重算 catalog 里这些包的 Crc/Size
    # ========================================================
    #
    # 为什么必须重算
    # --------------
    # catalog 的 Crc/Size 是在 copy_exclusion_bundles 阶段算的，
    # 那时用的是 exclusions 里的【原始包】。
    # rebake 会把 replacement/ 的贴图叠加进去，文件体积变了，
    # 但 catalog 仍记着旧值，于是：
    #
    #   uploadR2     上传 rebake 后的新文件（大）
    #   reconcileR2  对比 catalog 旧值 -> 判定不一致
    #   用 exclusions 原始包"修复" R2 -> 把叠加的贴图覆盖掉
    #
    # 结果：R2 上是没有替换贴图的版本，界面回退。
    #
    # 这里在 rebake 之后重算 Crc/Size，让 catalog 与实际文件一致。
    # ========================================================

    # 无条件执行：即使这次没有 rebake，copy_modified_bundles 拷进来的包
    # 也可能与 catalog 记录不一致（替换图烘焙会改变体积）。
    if True:
        _recommitted = 0
        _catalog_entries = build_bundle_map(official_catalog)
        # 扫整个 modified/：rebake 改过的、copy_modified_bundles 拷进来的，
        # 都要重算。只扫 exclusions 会漏掉「替换图烘焙」类的包。
        _mod_dir = MODIFIED_DIR / "AssetBundles" / "Android"
        _scan = sorted(_mod_dir.glob("*.bundle")) if _mod_dir.is_dir() else []
        for _p_maybe in _scan:
            _name = _p_maybe.name
            _p = _p_maybe
            _e = _catalog_entries.get(_name)
            if _e is None:
                continue
            _h = hashlib.md5()
            with _p.open("rb") as _fh:
                while True:
                    _c = _fh.read(1024 * 1024)
                    if not _c:
                        break
                    _h.update(_c)
            _md5 = _h.hexdigest()
            _size = _p.stat().st_size
            if _e.get("Crc") != _md5 or int(_e.get("Size", -1)) != _size:
                print(
                    f"[CATALOG-RECOMPUTE] {_name}\n"
                    f"    Crc : {str(_e.get('Crc'))[:12]} -> {_md5[:12]}\n"
                    f"    Size: {_e.get('Size')} -> {_size}"
                )
                _e["Crc"] = _md5
                _e["Size"] = _size
                _recommitted += 1
        if _recommitted:
            save_json(official_catalog_path, official_catalog)
            print(f"[CATALOG-RECOMPUTE] 已更新 {_recommitted} 个包的记录")

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
    # 10. 就地改写 ExcelDB（文本层 + 角色名 + 学年标签）
    #
    # 这里刻意不再下载旧版 ExcelDB。
    # 原来的做法是「把旧库的整行 Bytes 按 Key 覆盖到新库」，它要求旧库与新库
    # 逐行对齐：官方一旦新增/删除行，updateExcelDB.py 里的
    #     if len(old_rows) != len(new_rows): print("[跳过]"); continue
    # 就会静默跳过，脚本正常退出，但一个字都没改 —— 客户端拿到的还是官方中文，
    # 而且没有任何报错提示。
    #
    # 现在改成直接在官方现役表上就地替换：不依赖旧库，没有对齐要求，
    # 每次都会做残留校验，改不动就直接报错。
    # ========================================================

    # ========================================================
    # 9.5 上游 409 ID 整行还原
    #
    # 与上游 dev-r2 的 updateExcelDB.py 一致：把旧版 ExcelDB 中指定
    # ID 的行整体覆盖到新表。覆盖的是剧情脚本/对话等大段文本。
    #
    # 加固：
    #   · 旧库下载失败 -> 跳过，继续跑后面的 applyTextPatch
    #   · 行数对不齐（官方增删行）-> 跳过该行，不静默假装成功
    #   · 还原会丢词（桃信/桃桃朋友/地下市场…）-> 已由 applyUpstreamRestore
    #     的缺词保护跳过那些行，再由 applyTextPatch 补日服词
    # ========================================================

    excel_old_path = ROOT_DIR / "ExcelDB_old.db"
    restored = 0
    try:
        print()
        print("[RESTORE] 下载旧版 ExcelDB（上游镜像）")
        download_bytes(OLD_EXCEL_DB_URL, excel_old_path)
        print(f"[RESTORE] 旧库 {excel_old_path.stat().st_size} B")
        run_script(
            "applyUpstreamRestore.py",
            str(excel_old_path),
            str(excel_new_path),
        )
        restored = 1
    except Exception as exc:  # noqa: BLE001
        print(f"[RESTORE][WARN] 旧库还原失败，跳过该步骤: {exc}")
        print("[RESTORE][WARN] 构建将继续，文本仍会应用角色名/SKIP/AUTO/变长替换")
    finally:
        if excel_old_path.exists():
            try:
                excel_old_path.unlink()
            except OSError:
                pass

    print()
    print("[TEXT] 就地改写 ExcelDB（文本/角色名/学年）")

    # 注意：这里刻意【不做】「规则没变就跳过」的优化。
    #
    # 原因：跳过改写会让 excel_new_path 停留在刚下载的官方原表，
    # 随后算出的是官方 MD5，manifest 记录官方 Crc，
    # 客户端看到 Crc 变回官方值，就会把已部署的篡改表退回官方版 ——
    # 等于把汉化撤销掉。
    #
    # 真正省流量的做法应该是「复用上一次产出的篡改表」，
    # 那需要把上次的 ExcelDB 也缓存起来，复杂度高、收益有限
    # （离线构建本来就要跑一遍，跳过省的是 CI 时间不是玩家流量）。
    run_script(
        "applyTextPatch.py",
        str(excel_new_path),
    )

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
