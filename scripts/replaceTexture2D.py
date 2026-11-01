from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import urllib.request
from pathlib import Path

import UnityPy
from PIL import Image


# ============================================================
# 路径
# ============================================================

ROOT_DIR = Path(__file__).resolve().parent.parent

REPLACEMENT_DIR = ROOT_DIR / "replacement"
OUT_DIR = ROOT_DIR / "out"
MODIFIED_DIR = ROOT_DIR / "modified"


# Bundle 名称末尾：
#
# -2026-06-04_assets_all_817837721
#
# 去掉这一部分后，与 replacement 文件夹名称进行精确匹配。
BUNDLE_SUFFIX_RE = re.compile(
    r"-\d{4}-\d{2}-\d{2}_assets_all_\d+$",
    re.IGNORECASE,
)


# ============================================================
# Bundle 名称
# ============================================================

def normalize_bundle_name(bundle_path: Path) -> str:
    """
    例如：

    uis-01_common-03_nonequipment-_mxload-textures-2026-06-04_assets_all_817837721.bundle

    ->
    
    uis-01_common-03_nonequipment-_mxload-textures
    """

    return BUNDLE_SUFFIX_RE.sub("", bundle_path.stem)


# ============================================================
# 找 Bundle
# ============================================================

def find_bundle_candidates(
    replacement_name: str,
    bundle_files: list[Path],
) -> list[Path]:
    """
    精确匹配 replacement 文件夹名称。

    不使用 startswith，避免：
        abc
        abc-xxx
        abc-def

    之间发生误匹配。
    """

    replacement_name_lower = replacement_name.lower()

    return [
        bundle
        for bundle in bundle_files
        if normalize_bundle_name(bundle).lower()
        == replacement_name_lower
    ]


# ============================================================
# 从官方 Catalog 找并提供缺失的 Bundle
# ============================================================
#
# 背景（这是本仓库长期存在的一个真实缺陷）：
#
# update_resources.py 只在「官方 Bundle 与本地版本有差异」时才把 bundle 下载到
# 仓库根目录，然后才调用 replaceTexture2D.py。但官方经常只推 TableVersion、
# 不动 AssetBundle —— 此时差异为 0，根目录下没有任何 .bundle，
# replaceTexture2D.py 会以「没有找到任何 .bundle」退出码 1 结束，
# 于是 replacement/ 里的 96 张替换图永远不会被烘焙，R2 上的 UI 就一直是中文。
#
# 修法：replacement 里的每个文件夹都对应一个确定的 bundle 名。若根目录没有，
# 就按官方 Catalog 查出该 bundle 的下载地址，下下来再烘焙。这样视觉层不再依赖
# 「官方恰好更新了那个 bundle」。

CATALOG_URL_MAX = (
    "https://static.bluearchive-cn.com"
    "/prodm39/AssetBundles/Catalog"
)


def load_bundle_index() -> dict[str, dict] | None:
    """
    读取当前版本的官方 bundle 清单，返回 {规范化名字: 条目}。

    国服实际用的是 bundleDownloadInfo.json（不是 Unity 的 catalog_Android.json），
    结构形如 {"BundleFiles": [{"Name": ..., "Size": ..., "Crc": ...}]}。

    这里优先直接向官方拉取。不优先用本地副本，是因为 update_resources.py 在
    调用本脚本时，modified/ 目录才刚刚开始填充，本地那份的来源不可靠 ——
    实测在 CI 上会读到与本地不同的清单，导致同名 bundle 匹配到错误的版本
    （例如 common-03 拿到 24 KB 的增量补丁而不是 14 MB 的主资源包）。
    统一走官方源，本地开发与 CI 行为才一致。
    """

    version_file = ROOT_DIR / "current.txt"
    resource_version = ""
    if version_file.exists():
        for line in version_file.read_text(encoding="utf-8").splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                if k.strip() == "ResourceVersion":
                    resource_version = v.strip()

    payload = None
    if resource_version:
        url = f"{CATALOG_URL_MAX}/{resource_version}/Android/bundleDownloadInfo.json"
        try:
            with urllib.request.urlopen(url, timeout=90) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            print(f"  [INFO] 已从官方拉取清单 (ResourceVersion={resource_version})")
        except Exception as exc:
            print(f"  [WARN] 拉取官方清单失败: {exc}")

    if payload is None:
        # 网络不可用时才退回本地副本
        candidates = []
        if resource_version:
            candidates.append(
                MODIFIED_DIR / "AssetBundles" / "Catalog" / resource_version
                / "Android" / "bundleDownloadInfo.json"
            )
        candidates.append(ROOT_DIR / "bundleDownloadInfo.json")
        for path in candidates:
            if path.exists():
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    print(f"  [INFO] 回退使用本地清单: {path}")
                    break
                except Exception:
                    continue

    if not payload:
        return None

    entries = payload.get("BundleFiles") or []

    # 兼容 Unity 原生 catalog 形状（万一以后换回）
    if not entries:
        ids = payload.get("m_InternalIds") or []
        entries = [{"Name": Path(u).name} for u in ids if isinstance(u, str)]

    print(f"  [INFO] 清单条目数: {len(entries)}")

    index: dict[str, dict] = {}
    for entry in entries:
        name = str(entry.get("Name") or entry.get("name") or "")
        if not name:
            continue
        stem = BUNDLE_SUFFIX_RE.sub("", Path(name).stem).lower()

        # 同名 bundle 会存在多个版本，例如
        #   uis-01_common-03_nonequipment-...-2026-06-04_assets_all_2254605384.bundle  (14 MB，主资源)
        #   uis-01_common-03_nonequipment-...-2026-07-23_assets_all_3238474603.bundle  (8 KB，补丁)
        #   uis-01_common-03_nonequipment-...-2026-09-17_assets_all_1976541592.bundle  (25 KB，补丁)
        # 后两个是增量覆盖包，体积小、只含少量对象。要烘焙纹理必须拿主资源那个，
        # 否则会出现「明明匹配到了 bundle，却一张图都没替换」。
        previous = index.get(stem)
        if previous is not None:
            old_size = int(previous.get("Size") or 0)
            new_size = int(entry.get("Size") or 0)
            if old_size >= new_size:
                continue
        index[stem] = entry
    return index


def download_missing_bundle(
    replacement_name: str,
    bundle_index: dict[str, dict] | None,
) -> Path | None:
    """根目录没有对应 bundle 时，按官方清单下载一个。"""

    if not bundle_index:
        return None

    wanted = replacement_name.lower()
    entry = bundle_index.get(wanted)
    if not entry:
        return None

    name = str(entry.get("Name") or entry.get("name"))
    target = ROOT_DIR / name

    # 根目录若已存在（可能是 update_resources 刚下的官方新版），直接复用
    if target.exists():
        return target

    # update_resources.py 已下载过的副本也可能在 modified/ 下
    for base in (MODIFIED_DIR / "AssetBundles" / "Android",):
        alt = base / name
        if alt.exists():
            shutil.copy2(alt, target)
            print(f"  [FETCH] 从 modified 复用: {name}")
            return target

    url = (
        "https://static.bluearchive-cn.com"
        f"/prodm39/AssetBundles/Android/{name}"
    )
    try:
        with urllib.request.urlopen(url, timeout=300) as resp:
            data = resp.read()
    except Exception as exc:
        print(f"  [WARN] 下载 {name} 失败: {exc}")
        return None

    # 有 Crc 就校验，防止半截文件被当成成品烘焙进去
    expected = str(entry.get("Crc") or entry.get("crc") or "").lower()
    if expected:
        actual = hashlib.md5(data).hexdigest()
        if actual != expected:
            print(
                f"  [WARN] {name} 校验不符 (期望 {expected[:8]} 实际 {actual[:8]})，跳过"
            )
            return None

    target.write_bytes(data)
    print(f"  [FETCH] 下载缺失 bundle: {name} ({len(data)} B)")
    return target



# ============================================================
# Texture2D 索引
# ============================================================

def build_texture_index(env) -> dict[str, object]:
    """
    建立：

        Texture2D.m_Name -> ObjectReader

    """

    texture_index: dict[str, object] = {}

    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue

        try:
            name = obj.peek_name()

        except Exception:
            try:
                data = obj.read()
                name = data.m_Name
            except Exception as exc:
                print(
                    f"    [WARNING] 无法读取 Texture2D "
                    f"path_id={obj.path_id}: {exc}"
                )
                continue

        if not name:
            continue

        # 同一个 Bundle 中如果真的存在同名 Texture2D，
        # 这里不主动选择其中一个，避免误替换。
        if name in texture_index:
            raise RuntimeError(
                f"Bundle 中存在多个同名 Texture2D: {name}"
            )

        texture_index[name] = obj

    return texture_index


# ============================================================
# 替换单张图片
# ============================================================

def replace_texture(
    texture_obj,
    image_path: Path,
) -> None:
    """
    使用 PNG 替换 Texture2D。
    """

    texture = texture_obj.read()

    with Image.open(image_path) as img:
        img.load()
        replacement_image = img.copy()

    texture.image = replacement_image
    texture.save()


# ============================================================
# 处理一个 Bundle
# ============================================================

def process_bundle(
    bundle_path: Path,
    replacement_dir: Path,
) -> int:

    print()
    print("=" * 72)
    print(f"Bundle      : {bundle_path}")
    print(f"Replacement : {replacement_dir}")
    print("=" * 72)

    # --------------------------------------------------------
    # 查找 PNG
    # --------------------------------------------------------

    png_files = sorted(
        p
        for p in replacement_dir.rglob("*")
        if p.is_file()
        and p.suffix.lower() == ".png"
    )

    if not png_files:
        print("  [WARNING] replacement 文件夹中没有 PNG")
        return 0

    print(f"  PNG 数量: {len(png_files)}")

    # --------------------------------------------------------
    # 加载 Bundle
    # --------------------------------------------------------

    print("  正在加载 Bundle...")

    env = UnityPy.load(str(bundle_path))

    # --------------------------------------------------------
    # 建立 Texture2D 索引
    # --------------------------------------------------------

    texture_index = build_texture_index(env)

    print(
        f"  Bundle 中 Texture2D 数量: {len(texture_index)}"
    )

    replaced_count = 0
    skipped_count = 0

    # --------------------------------------------------------
    # 逐个 PNG 替换
    # --------------------------------------------------------

    for image_path in png_files:

        texture_name = image_path.stem

        texture_obj = texture_index.get(texture_name)

        # ----------------------------------------------------
        # 找不到对应 Texture2D：跳过
        # ----------------------------------------------------

        if texture_obj is None:
            print(
                f"  [SKIP] {image_path.name}"
                f" -> Bundle 中不存在同名 Texture2D"
            )
            skipped_count += 1
            continue

        try:
            texture = texture_obj.read()

            with Image.open(image_path) as img:
                width, height = img.size

            print(
                f"  [REPLACE] {image_path.name} "
                f"({width}x{height})"
            )

            replace_texture(
                texture_obj,
                image_path,
            )

            replaced_count += 1

        except Exception as exc:
            raise RuntimeError(
                f"替换 Texture2D 失败:\n"
                f"  Bundle: {bundle_path}\n"
                f"  Texture2D: {texture_name}\n"
                f"  PNG: {image_path}\n"
                f"  Error: {type(exc).__name__}: {exc}"
            ) from exc

    # --------------------------------------------------------
    # 没有任何修改
    # --------------------------------------------------------

    if replaced_count == 0:

        print(
            f"  [NO CHANGE] "
            f"没有任何 Texture2D 被替换"
        )

        return 0

    # --------------------------------------------------------
    # 输出路径
    # --------------------------------------------------------
    #
    # 保留 Bundle 相对于 ROOT_DIR 的目录结构。
    #
    # 例如：
    #
    # ./some/path/foo.bundle
    #
    # ->
    #
    # ./out/some/path/foo.bundle
    #
    # --------------------------------------------------------

    relative_path = bundle_path.relative_to(ROOT_DIR)

    output_path = OUT_DIR / relative_path

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    # --------------------------------------------------------
    # 保存
    # --------------------------------------------------------

    print(f"  正在写入:")
    print(f"    {output_path}")

    with output_path.open("wb") as f:
        f.write(
            env.file.save(
                packer="original"
            )
        )

    print(
        f"  [DONE] "
        f"替换 {replaced_count} 个，"
        f"跳过 {skipped_count} 个"
    )

    return replaced_count


# ============================================================
# 主程序
# ============================================================

def main() -> int:

    print("=" * 72)
    print("UnityPy Texture2D Replacement")
    print("=" * 72)

    print(f"ROOT        : {ROOT_DIR}")
    print(f"REPLACEMENT : {REPLACEMENT_DIR}")
    print(f"OUTPUT      : {OUT_DIR}")
    print(
        f"UnityPy     : "
        f"{getattr(UnityPy, '__version__', 'unknown')}"
    )

    # --------------------------------------------------------
    # 检查 replacement
    # --------------------------------------------------------

    if not REPLACEMENT_DIR.is_dir():
        print(
            f"[ERROR] replacement 不存在:\n"
            f"  {REPLACEMENT_DIR}",
            file=sys.stderr,
        )
        return 1

    replacement_dirs = sorted(
        p
        for p in REPLACEMENT_DIR.iterdir()
        if p.is_dir()
    )

    if not replacement_dirs:
        print(
            "[ERROR] replacement 中没有子文件夹",
            file=sys.stderr,
        )
        return 1

    # --------------------------------------------------------
    # 查找全部 Bundle
    # --------------------------------------------------------
    #
    # 排除：
    #   replacement/
    #   scripts/
    #   out/
    #
    # 避免重复处理已经生成的 Bundle。
    # --------------------------------------------------------

    excluded_dirs = {
        REPLACEMENT_DIR.resolve(),
        (ROOT_DIR / "scripts").resolve(),
        OUT_DIR.resolve(),
        (ROOT_DIR / "assetexclusions").resolve(),
        (ROOT_DIR / "modified").resolve(),
    }

    bundle_files: list[Path] = []

    for path in ROOT_DIR.rglob("*.bundle"):

        if not path.is_file():
            continue

        resolved = path.resolve()

        if any(
            excluded == resolved
            or excluded in resolved.parents
            for excluded in excluded_dirs
        ):
            continue

        bundle_files.append(path)

    bundle_files.sort()

    # 「根目录没有任何 .bundle」不再是致命错误：
    # 下面会按需从官方 Catalog 下载 replacement 需要的那几个。
    if not bundle_files:
        print()
        print("[INFO] 根目录没有现成 .bundle，将按 replacement 需求从官方下载")

    print()
    print(f"Bundle 数量       : {len(bundle_files)}")
    print(f"Replacement 数量  : {len(replacement_dirs)}")

    catalog = load_bundle_index()
    if catalog is None:
        print("[WARN] 读不到官方 bundle 清单，缺失的 bundle 将无法补齐")

    # --------------------------------------------------------
    # 统计
    # --------------------------------------------------------

    matched_dirs = 0
    skipped_no_bundle = 0
    processed_bundles = 0
    total_replaced = 0

    # --------------------------------------------------------
    # 处理每个 replacement 文件夹
    # --------------------------------------------------------

    for replacement_dir in replacement_dirs:

        replacement_name = replacement_dir.name

        print()
        print("-" * 72)
        print(f"[MATCH] {replacement_name}")

        candidates = find_bundle_candidates(
            replacement_name,
            bundle_files,
        )

        # ----------------------------------------------------
        # 根目录没有这个 Bundle —— 主动从官方下载
        #
        # 官方只推 TableVersion 时，update_resources.py 的差异集为空，
        # 根目录下不会有任何 .bundle，这里若直接跳过，replacement 里的
        # 替换图就永远烘焙不进去（R2 上的 UI 会一直显示中文）。
        # ----------------------------------------------------

        if not candidates:
            fetched = download_missing_bundle(
                replacement_name,
                catalog,
            )
            if fetched is not None:
                candidates = [fetched]
                bundle_files.append(fetched)

        # ----------------------------------------------------
        # 仍然没有对应 Bundle
        # ----------------------------------------------------

        if not candidates:
            print(
                f"  [SKIP] 没有找到对应 Bundle，且官方 Catalog 中也没有"
            )
            skipped_no_bundle += 1
            continue

        matched_dirs += 1

        print(
            f"  找到 {len(candidates)} 个候选 Bundle:"
        )

        for bundle in candidates:
            print(f"    - {bundle}")

        # ----------------------------------------------------
        # 逐个 Bundle 检查
        # ----------------------------------------------------

        for bundle_path in candidates:

            try:
                replaced = process_bundle(
                    bundle_path,
                    replacement_dir,
                )

                if replaced > 0:
                    processed_bundles += 1
                    total_replaced += replaced

            except Exception as exc:

                print(
                    f"\n[ERROR] Bundle 处理失败:\n"
                    f"{exc}",
                    file=sys.stderr,
                )

                return 1

    # --------------------------------------------------------
    # 总结
    # --------------------------------------------------------

    print()
    print("=" * 72)
    print("处理完成")
    print("=" * 72)

    print(
        f"Replacement 文件夹 : {len(replacement_dirs)}"
    )

    print(
        f"匹配到 Bundle 的文件夹 : {matched_dirs}"
    )

    print(
        f"跳过无 Bundle 的文件夹 : {skipped_no_bundle}"
    )

    print(
        f"生成输出 Bundle      : {processed_bundles}"
    )

    print(
        f"替换 Texture2D       : {total_replaced}"
    )

    print(
        f"输出目录             : {OUT_DIR}"
    )

    print("=" * 72)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())