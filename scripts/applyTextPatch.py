#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
applyTextPatch.py —— 直接在官方现役 ExcelDB 上就地替换文本与角色名

与旧的 updateExcelDB.py 的本质区别
----------------------------------
updateExcelDB.py 的做法是「下载一个旧版 ExcelDB，按 Key 列表把旧版的整行 Bytes
覆盖到新版」。它依赖旧库与新版逐行对齐：一旦官方新增或删除行，脚本按 primary_key
查到的行数对不上，就会 print("[跳过]") 然后静默continue —— 结果是没有报错、
但什么都没改，客户端拿到的仍是官方中文。

本脚本改成「就地重写」：
  * 文本层（LocalizeDBSchema）：解出字符串 -> 应用替换表 -> 按 4 字节对齐重建 blob
  * 名字层（全库）：等长字节替换（UTF-8 字节数必须相同，偏移不变，flatbuffer 不坏）
  * 学年层（LocalizeCharacterProfileDBSchema）：等长替换，且只动角色资料页，
    剧情台词里的「第1年」是叙事语境，保持原样

不依赖任何旧库，因此没有「行数不一致就静默跳过」的失效模式。
每次执行结束都会做残留校验，有残留直接抛错，不会把半成品推上去。
"""

from __future__ import annotations

import hashlib
import sqlite3
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from renameGeng import (  # noqa: E402
    rename_geng, apply_equal_len, check_equal_len, find_slot, SKIP_TABLES,
)

# ============================================================
# 替换规则
# ============================================================

# 文本层：按长度前缀重写，允许不等长。
# 顺序敏感 —— 「好感等级」必须先于「等级」规则生效，否则会被拆成「好感Lv.」。
TEXT_RULES = [
    ("好感等级", "\x01BOND\x01"),   # 占位，最后还原为「羁绊等级」
    ("桃桃朋友", "MomoFriends"),
    ("桃信", "MomoTalk"),
    ("成员", "学员"),
    ("概率提升", "Pick Up"),
    ("最大值", "MAX"),
    ("最小值", "MIN"),
    ("评级", "Rank."),
    ("地下市场", "黑市"),
    ("快速战斗", "扫荡"),
    ("必杀技能", "EX技能"),
    ("基础技能", "基本技能"),
    ("被动技能", "强化技能"),
    ("辅助技能", "子技能"),
    ("技能升级", "技能成长"),
    # 角色名「庚」->「鹿江」。LocalizeDBSchema 是独立布局（40 字节头 +
    # 长度前缀 + 4 字节对齐填充），变长必须走这里按长度重建。
    # 其它表（flatbuffer 槽）交给 renameGeng，两者范围互不重叠。
    ("庚", "鹿江"),
]

# 仅当整条字符串完全等于左侧时才替换。
# 这条规则很关键：「等级」在表里是 14 个界面共用的标签，但「学员等级」
# 「必杀技能等级」「技能等级」是各自独立的完整字符串，全局替换会把它们
# 一并改成「学员Lv.」，那是错的。所以只认整条相等。
EXACT_RULES = {
    "公告": "Notice",
    "未命中": "MISS",
    "等级": "Lv.",
    # 属性面板：整条相等才换，避免污染「体力上限」「攻击力加成」等长句
    "体力": "HP",
    "攻击力": "ATK",
    "防御力": "DEF",
    "治疗力": "治愈力",
    # 属性加成的合成词——它们本身是独立完整字符串，所以要单独列。
    # 关键：`体力上限加成`(18字节) 是比 `体力上限`(12字节) 更长的独立条目，
    # 只写后者会漏掉它（游戏内「能力解放」页显示的正是这个长词）。
    "体力上限加成": "HP上限加成",
    "体力上限": "HP上限",
    "攻击力加成": "ATK加成",
    "治疗力加成": "治愈力加成",
}

BOND = "羁绊等级"

# 战斗属性：只在短标签（len<=12）里替换，避免污染长句里的「命中判定」等
COMBAT_RULES = [("暴击", "CRIT"), ("闪避", "EVA"), ("命中", "HIT")]
COMBAT_FIX = [
    ("CRIT率", "CRIT"), ("CRIT值", "CRIT"),
    ("EVA率", "EVA"), ("EVA值", "EVA"),
    ("HIT率", "HIT"), ("HIT值", "HIT"),
]

# 名字层：全库等长字节替换。
# 必须等长 —— 这些字符串嵌在 MemoryPack/flatbuffer 里，改长度会让后续字段
# 的偏移错位，轻则名字显示不全，重则整个表解析失败。
NAME_RULES = [
    ("陆八魔爱露", "陆八魔阿露"),
    ("美甘妮露", "美甘尼禄"),
    ("水羽三森", "水羽弥守"),
    ("池仓真里奈", "池仓玛丽娜"),
    ("苍森美祢", "苍森美弥"),
    ("朱城瑠海", "朱城留美"),
    ("姬木芽瑠", "姬木梅露"),
    ("丰见琴里", "丰见小鸟"),
    ("里浜海香", "里浜海夏"),
    ("尾刃康娜", "尾刃叶渚"),
    ("牛牧朱莉", "牛牧茱莉"),
    ("栗村爱理", "栗村爱莉"),
    ("爱清风香", "爱清枫香"),
    ("夜樱绮罗罗", "夜樱绮良良"),
    # 姓与名是独立字段，必须单独列
    ("妮露", "尼禄"), ("爱露", "阿露"), ("三森", "弥守"),
    ("真里奈", "玛丽娜"), ("美祢", "美弥"), ("瑠海", "留美"),
    ("芽瑠", "梅露"), ("琴里", "小鸟"), ("海香", "海夏"),
    ("康娜", "叶渚"), ("朱莉", "茱莉"), ("爱理", "爱莉"),
    ("风香", "枫香"), ("绮罗罗", "绮良良"),
    # 名字改名已整体撤销，恢复官方原名「庚」（用户要求）。
]

# 学年标签：等长替换，仅角色资料表
YEAR_RULES = [("第1年", "1年生"), ("第2年", "2年生"), ("第3年", "3年生")]

# 执行完必须为 0 的旧词；非 0 说明规则漏了或顺序错了
MUST_BE_EMPTY = [
    "必杀技能", "基础技能", "被动技能", "辅助技能",
    "桃信", "桃桃朋友", "概率提升", "快速战斗", "地下市场",
    "妮露", "爱露", "风香", "苍森美祢", "绮罗罗",
]

# 绝不能被「等级 -> Lv.」误伤的复合词。
# 注意「必杀技能等级」不在此列：它会被「必杀技能 -> EX技能」正确改写成
# 「EX技能等级」，那是期望行为，不是误伤。
MUST_KEEP = ["学员等级", "技能等级"]


# ============================================================
# blob 处理
# ============================================================

def pad_for(n: int) -> int:
    """LocalizeDBSchema 的填充规则：总是补到 4 字节，整除时也补 4。"""
    return 4 - (n % 4) if n % 4 else 4


def parse_localize(blob) -> str | None:
    b = bytes(blob)
    if len(b) < 45:
        return None
    length = struct.unpack("<I", b[40:44])[0]
    if 44 + length + pad_for(length) != len(b):
        return None
    try:
        return b[44:44 + length].decode("utf-8")
    except Exception:
        return None


def rebuild_localize(blob, new_text: str) -> bytes:
    payload = new_text.encode("utf-8")
    return (
        bytes(blob)[:40]
        + struct.pack("<I", len(payload))
        + payload
        + b"\x00" * pad_for(len(payload))
    )


# ============================================================
# 主流程
# ============================================================

def apply_text_layer(cur) -> int:
    changed = 0
    for key, blob in cur.execute("SELECT Key, Bytes FROM LocalizeDBSchema").fetchall():
        if not isinstance(blob, (bytes, bytearray)):
            continue
        value = parse_localize(blob)
        if value is None:
            continue
        original = value
        for old, new in TEXT_RULES:
            value = value.replace(old, new)
        if len(original) <= 12:
            for old, new in COMBAT_RULES:
                value = value.replace(old, new)
            for old, new in COMBAT_FIX:
                value = value.replace(old, new)
        if original in EXACT_RULES:
            value = EXACT_RULES[original]
        value = value.replace("\x01BOND\x01", BOND)
        if value != original:
            cur.execute(
                "UPDATE LocalizeDBSchema SET Bytes=? WHERE Key=?",
                (rebuild_localize(blob, value), key),
            )
            changed += 1
    return changed


def apply_name_layer(cur) -> int:
    for old, new in NAME_RULES:
        if len(old.encode("utf-8")) != len(new.encode("utf-8")):
            raise RuntimeError(
                f"名字规则 {old!r} -> {new!r} 字节长度不等，"
                "会破坏 flatbuffer 偏移，已中止"
            )

    tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    changed = 0
    for table in tables:
        columns = [c[1] for c in cur.execute(f"PRAGMA table_info({table})")]
        if "Bytes" not in columns:
            continue
        for rowid, blob in cur.execute(f'SELECT rowid, Bytes FROM "{table}"').fetchall():
            if not isinstance(blob, (bytes, bytearray)):
                continue
            data = bytes(blob)
            hit = False
            for old, new in NAME_RULES:
                ob, nb = old.encode("utf-8"), new.encode("utf-8")
                if ob in data:
                    data = data.replace(ob, nb)
                    hit = True
            if hit:
                cur.execute(
                    f'UPDATE "{table}" SET Bytes=? WHERE rowid=?',
                    (sqlite3.Binary(data), rowid),
                )
                changed += 1
    return changed


def apply_year_layer(cur) -> int:
    for old, new in YEAR_RULES:
        if len(old.encode("utf-8")) != len(new.encode("utf-8")):
            raise RuntimeError(f"学年规则 {old!r} -> {new!r} 字节长度不等，已中止")

    changed = 0
    for cid, blob in cur.execute(
        "SELECT CharacterId, Bytes FROM LocalizeCharProfileDBSchema"
    ).fetchall():
        data = bytes(blob)
        hit = False
        for old, new in YEAR_RULES:
            if old.encode("utf-8") in data:
                data = data.replace(old.encode("utf-8"), new.encode("utf-8"))
                hit = True
        if hit:
            cur.execute(
                "UPDATE LocalizeCharProfileDBSchema SET Bytes=? WHERE CharacterId=?",
                (sqlite3.Binary(data), cid),
            )
            changed += 1
    return changed


def verify(db_path: Path) -> None:
    con = sqlite3.connect(str(db_path))
    values = [parse_localize(b) for (b,) in con.execute("SELECT Bytes FROM LocalizeDBSchema")]
    values = [v for v in values if v]

    left = [(w, sum(1 for v in values if w in v)) for w in MUST_BE_EMPTY]
    left = [(w, n) for w, n in left if n]
    if left:
        con.close()
        raise RuntimeError(f"残留未清零: {left}")

    broken = [w for w in MUST_KEEP if not any(v == w for v in values)]
    if broken:
        con.close()
        raise RuntimeError(f"这些复合词被误伤: {broken}")

    years = {
        w: sum(
            1
            for (b,) in con.execute("SELECT Bytes FROM LocalizeCharProfileDBSchema")
            if w.encode("utf-8") in bytes(b)
        )
        for w in ("1年生", "2年生", "3年生")
    }
    con.close()
    if sum(years.values()) < 200:
        raise RuntimeError(f"学年标签覆盖不足: {years}")


def _scan_leftover(cur, word: str = "庚"):
    """扫描全库仍含 word 的槽，返回 [(表, 键, 文本)]。用于收尾自检。"""
    wb = word.encode()
    out = []
    tabs = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for t in tabs:
        if t in SKIP_TABLES:
            continue
        cols = [c[1] for c in cur.execute('PRAGMA table_info("%s")' % t)]
        if "Bytes" not in cols:
            continue
        pk = None
        for c in cur.execute('PRAGMA table_info("%s")' % t):
            if c[5]:
                pk = c[1]
                break
        sel = ('"%s"' % pk) if pk else "rowid"
        for key, blob in cur.execute('SELECT %s, Bytes FROM "%s"' % (sel, t)).fetchall():
            if not isinstance(blob, (bytes, bytearray)):
                continue
            data = bytes(blob)
            pos = 0
            while True:
                i = data.find(wb, pos)
                if i < 0:
                    break
                slot = find_slot(data, i, len(wb))
                if slot and word in slot[2]:
                    out.append((t, key, slot[2]))
                pos = i + len(wb)
    return out


def read_change_counter(path: Path) -> bytes:
    """SQLite 文件头 offset 98..99 的 file change counter。"""
    with path.open("rb") as fh:
        fh.seek(98)
        return fh.read(2)


def write_change_counter(path: Path, value: bytes) -> None:
    with path.open("r+b") as fh:
        fh.seek(98)
        fh.write(value)


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if target is None:
        print("用法: applyTextPatch.py <ExcelDB.db>")
        return 2
    if not target.exists():
        print(f"[TEXT] 找不到 {target}")
        return 2

    print(f"[TEXT] 就地改写 {target.name}")

    con = sqlite3.connect(str(target))
    cur = con.cursor()

    text_changed = apply_text_layer(cur)
    con.commit()
    print(f"[TEXT] 文本层      : {text_changed} 行")

    name_changed = apply_name_layer(cur)
    con.commit()
    print(f"[TEXT] 名字层      : {name_changed} 行（全库）")

    year_changed = apply_year_layer(cur)
    con.commit()
    print(f"[TEXT] 学年层      : {year_changed} 个角色")

    # 名字改名必须放最后：它会改变行内字节布局（吃零填充 / 行末变长）。
    # 放在其它层之后，前面的规则仍按官方原始布局匹配，不会互相干扰。
    rows, cnt, left = rename_geng(cur)
    con.commit()
    print(
        "[TEXT] 改名层      : %d 行（吃零填充 %d / 行末变长 %d / 缩小 %d）"
        % (rows, cnt["A"], cnt["B"], cnt["S"])
    )
    eq = apply_equal_len(cur)
    con.commit()
    print(f"[TEXT] 等字节重写  : {eq} 处")

    # 收尾自检：仍有「庚」说明规则没覆盖到，直接抛错，不推半成品
    leftover = [s for _, _, s in _scan_leftover(cur)]
    if leftover:
        con.close()
        raise RuntimeError("仍有「庚」未处理: %r" % leftover[:4])

    # 不做 VACUUM：VACUUM 会重排页布局，改变最终 MD5 和文件大小，
    # 导致同一份官方表每次构建产出不同的 Crc（客户端会误判为又更新了）。
    # 保持原页布局，让构建结果严格可复现。
    con.commit()
    con.close()

    # SQLite 文件头里有一组「与内容无关、却随写入自增」的计数器：
    #   offset 24..27 文件格式版本号
    #   offset 92..95 version-valid-for
    #   offset 96..99 SQLite version number（含 98..99 的 file change counter）
    # 不固定它们，同一份数据每次构建都会产出不同 MD5，客户端误判为
    # 「官方又更新了」，白白重下 153 MB。这里统一写 0：只要内容一致，
    # Crc 就一致，构建严格可复现。
    # （客户端每次都从空白加载这个文件，不存在需要靠自增来失效缓存的连接。）
    for start, length in ((24, 4), (92, 4), (96, 4)):
        with target.open("r+b") as fh:
            fh.seek(start)
            fh.write(b"\x00" * length)

    print("[TEXT] 校验…")
    verify(target)

    digest = hashlib.md5()
    with target.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    print(f"[TEXT] 新 MD5 : {digest.hexdigest()}")
    print(f"[TEXT] 新 Size: {target.stat().st_size}")
    print("[TEXT] 完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
