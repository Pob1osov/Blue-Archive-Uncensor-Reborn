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
    # 2026-11-17 决定：接入上游 409 ID 还原后，还原会把某些词直接删掉
    # （「尝试用桃信进行联络」-> 「尝试用进行联络」），出现缺词。
    # 因此这里补回日服词，保证句子完整：
    #   上游删词 -> 我们用日服说法占位，而不是留空。
    ("桃桃朋友", "MomoFriends"),
    ("桃信", "MomoTalk"),
    ("地下市场", "黑市"),
    ("快速战斗", "扫荡"),
    ("最小值", "MIN"),
    ("最大值", "MAX"),
    ("信任等级", "羁绊等级"),
    ("评级", "Rank."),
    # 「庚」在 LocalizeDBSchema 里是独立布局，变长必须走文本层按长度重建；
    # flatbuffer 槽由 FLATBUFFER_FLEX_RULES 负责。
    ("庚", "鹿江"),
]
EXACT_RULES = {
    # 2026-11-16 决定：界面文本只保留这两条，其余照搬官方。
    "自动": "AUTO",
    "跳过": "SKIP",
}
BOND = "羁绊等级"
LEVEL_WORD = None   # 「等级 -> Lv.」已停用
LEVEL_REPLACEMENT = None
LEVEL_PROTECT = ()
COMBAT_RULES = []
COMBAT_FIX = []
NAME_RULES = [
    # 「（武装）」-> 「（临战）」
    #
    # 放在最前面，因为它作用于「角色名里的形态后缀」，与后面的具体人名无关。
    #
    # 为什么用「全库等长字节替换」而不是 TEXT_RULES
    # ------------------------------------------------
    # 这是一个纯字节替换：两者都是 12 字节（全角括号 3 字节 + 两个汉字 6 字节
    # + 全角括号 3 字节），偏移不变，flatbuffer / MemoryPack 结构完全不受影响。
    #
    # 为什么它能自动覆盖未来新角色
    # ----------------------------
    # 规则只匹配「（武装）」这个后缀本身，不列举角色名。
    # 将来上线「爱丽丝（武装）」，其名称在库里同样是「爱丽丝（武装）」，
    # 这个字节模式会被直接命中 -> 显示为「爱丽丝（临战）」，
    # 无需为新角色追加任何规则。
    #
    # 为什么不会误伤正文
    # ------------------
    # 匹配的是带全角括号的完整后缀「（武装）」，
    # 剧情里单独出现的「武装」二字（如「武装起来」）不会被命中。
    ("（武装）", "（临战）"),
    ("沙勒", "夏莱"),
    ("崔尼蒂", "圣三一"),
    ("格武拉", "盖布拉"),
    ("歌赫娜", "格黑娜"),
    # 以上四组为地域/组织名，等长替换（UTF-8 字节数相同），
    # 走全库字节替换可覆盖剧情、技能、人名等所有表，安全。
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
YEAR_RULES = []
FLATBUFFER_FLEX_RULES = [
    # 2026-11-16 重新启用。旧实现漏写字符串终止符（4+len(payload) 恰为 4 的
    # 倍数时 pad 算出 0），163 个追加串里有 44 个没有 0x00 结尾，正是
    # GameDataInitializer.LoadInBatches 抛 ObjectDisposedException 的真因。
    # 终止符已改为显式写入，并由 check_flex_terminators() 在写回前逐串校验。
    ("提翡瑞", "提法瑞特"),
    ("庚", "鹿江"),
]

# 执行完必须为 0 的旧词；非 0 说明规则漏了或顺序错了
#
# 注意：这里只校验 LocalizeDBSchema（文本层）。
# 「（武装）」这类改动走的是 apply_name_layer 的全库字节替换，
# 由 verify_form_suffix() 单独校验，不要加到这里——否则会误报。
MUST_BE_EMPTY = [
    # 角色名层执行后必须为 0 的旧名。
    # 「成员」「等级」等已不再替换，若列在这里会误报。
    "妮露", "爱露", "风香", "苍森美祢", "绮罗罗",
]
MUST_KEEP = []
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
# flatbuffer 变长替换
# ============================================================
#
# 为什么需要这一层
# ----------------
# 有些术语长度会变（提翡瑞 9->12 字节、庚 3->6 字节），
# 而它们散落在 ScenarioScript / LocalizeSkill 等多张表里。
# 这些表的 blob 是 flatbuffer，直接改字节长度会让后续数据偏移全错。
#
# 解决办法（两种，都不移动已有偏移）
# ----------------------------------
#   1) 字符串被根表字段偏移引用：
#      把新串追加到 blob 末尾，只改字段偏移指向新位置，原串留作垃圾数据。
#   2) 字符串贴在 blob 末尾且前面是长度前缀：
#      原地改长度前缀 + 替换内容 + 补 \0（后面没有数据，不影响任何偏移）。
#
# 实测覆盖：提翡瑞 88/88、庚 60/61（剩 1 处是长句里的普通词，不应改）。

def _fb_u32(d, o):
    import struct
    return struct.unpack("<I", d[o:o + 4])[0]


def _fb_s32(d, o):
    import struct
    return struct.unpack("<i", d[o:o + 4])[0]


def _fb_try_str(d, target):
    """在 target 附近解析字符串，返回 (内容起始, 长度) 或 None。

    实测本项目 flatbuffer 的语义是：
        target = 字符串【内容起点】，长度前缀在 target-4

    但为稳妥也兼容「长度前缀在 target」的布局。
    只接受可打印文本，避免把二进制数组误判成字符串。
    """

    def _ok(pos, ln):
        try:
            txt = d[pos:pos + ln].decode("utf-8")
        except Exception:
            return False
        for ch in txt:
            o = ord(ch)
            if o < 0x20 and ch not in "\n\t":
                return False
        return True

    if target >= 4:
        ln = _fb_u32(d, target - 4)
        if 1 <= ln <= 1024 and target + ln <= len(d) and _ok(target, ln):
            return target, ln
    if target + 4 <= len(d):
        ln = _fb_u32(d, target)
        if 1 <= ln <= 1024 and target + 4 + ln <= len(d) and _ok(target + 4, ln):
            return target + 4, ln
    return None


def _fb_string_fields(d):
    """遍历根表引用的字符串字段，产出 (字段值位置, 内容起始, 长度)。"""
    import struct
    if len(d) < 8:
        return
    try:
        root = _fb_u32(d, 0)
        if root + 4 > len(d):
            return
        soff = _fb_s32(d, root)
        vtoff = root - soff
        if not (0 <= vtoff < len(d) - 4):
            return
        vtsize = struct.unpack("<H", d[vtoff:vtoff + 2])[0]
        if vtsize < 4 or vtoff + vtsize > len(d):
            return
        nfields = (vtsize - 4) // 2
        for fi in range(nfields):
            voff = struct.unpack("<H", d[vtoff + 4 + fi * 2: vtoff + 6 + fi * 2])[0]
            if voff == 0:
                continue
            fpos = root + voff
            if fpos + 4 > len(d):
                continue
            target = fpos + _fb_u32(d, fpos)
            if not (4 <= target < len(d)):
                continue
            r = _fb_try_str(d, target)
            if r is not None:
                yield fpos, r[0], r[1]
    except Exception:
        return


def check_flex_terminators(cur) -> list:
    """校验变长层：每个被替换的串都要能按 flatbuffers 规则读回新词。

    检查三件事，任一失败都算严重错误：
      1. 追加串以 0x00 收尾（缺了客户端会越界读）
      2. 追加串起点 4 字节对齐
      3. 按字段 uoffset 解析，能读出期望的新字符串
    必须返回空列表才允许写回。
    """
    import struct
    bad = []
    if not FLATBUFFER_FLEX_RULES:
        return bad
    tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for table in tables:
        cols = [c[1] for c in cur.execute('PRAGMA table_info("%s")' % table)]
        if "Bytes" not in cols:
            continue
        for rowid, blob in cur.execute('SELECT rowid, Bytes FROM "%s"' % table).fetchall():
            if not isinstance(blob, (bytes, bytearray)):
                continue
            d = bytes(blob)
            for fpos, sstart, slen in _fb_string_fields(d):
                try:
                    txt = d[sstart:sstart + slen].decode("utf-8")
                except Exception:
                    continue
                if not any(new in txt for _, new in FLATBUFFER_FLEX_RULES):
                    continue
                # 1) 终止符
                if sstart + slen >= len(d) or d[sstart + slen] != 0:
                    bad.append((table, rowid, "缺少 0x00 终止符", fpos, sstart, txt))
                    continue
                # 2) 对象起点对齐
                obj = sstart - 4
                if obj < 4 or obj % 4:
                    bad.append((table, rowid, "长度前缀未 4 字节对齐 obj=%d" % obj,
                                fpos, sstart, txt))
                    continue
                # 3) 字段 uoffset 必须正好指向长度前缀
                uoff = struct.unpack_from("<I", d, fpos)[0]
                if fpos + uoff != obj:
                    bad.append((table, rowid,
                                "uoffset 指向错误 %d!=%d" % (fpos + uoff, obj),
                                fpos, sstart, txt))
                    continue
                if struct.unpack_from("<I", d, obj)[0] != slen:
                    bad.append((table, rowid, "长度前缀不匹配", fpos, sstart, txt))
    return bad


def patch_flatbuffer(blob, rules):
    """对单个 flatbuffer blob 应用变长替换，返回 (新blob, 替换数)。

    策略：新串追加到 blob 末尾，只改字段偏移指向新内容起点。
    所有已有偏移保持不动，结构必然完整。
    """
    import struct
    d = bytearray(blob)
    total = 0

    fields = list(_fb_string_fields(bytes(d)))
    if not fields:
        return bytes(blob), 0

    appended = bytearray()
    for fpos, sstart, slen in fields:
        try:
            txt = bytes(d[sstart:sstart + slen]).decode("utf-8")
        except Exception:
            continue

        new_txt = txt
        for old, new in rules:
            if old in new_txt:
                new_txt = new_txt.replace(old, new)
        if new_txt == txt:
            continue

        payload = new_txt.encode("utf-8")
        base = len(d) + len(appended)
        # 追加串起点先对齐到 4 字节（flatbuffers 要求长度前缀按 4 字节对齐）
        if base % 4:
            pad = (-base) % 4
            appended += b"\x00" * pad
            base += pad
        # flatbuffers 字符串必须是「4B 长度 + UTF-8 正文 + 至少一个 0x00」。
        # 旧写法把终止符算进了补位：len(4+payload) 恰为 4 的倍数时 pad=0，
        # 结果一个 0x00 都不写，客户端读完正文会越界继续读，
        # 表现为 GameDataInitializer.LoadInBatches 抛异常 / 卡「正在初始化游戏数据」。
        # 现在终止符显式写入，补位只负责把整个 entry 对齐到 4 字节。
        entry = struct.pack("<I", len(payload)) + payload + b"\x00"
        entry += b"\x00" * ((-len(entry)) % 4)
        appended += entry

        # uoffset 必须指向【字符串对象起点】（4B 长度前缀所在处），
        # 不是正文起点。旧写法多加了 4，指针落到正文上，
        # 客户端把 UTF-8 正文当长度读，于是整条记录解析崩掉。
        new_content = base
        struct.pack_into("<I", d, fpos, (new_content - fpos) & 0xFFFFFFFF)
        total += 1

    if appended:
        d += appended

    return bytes(d), total


def apply_flatbuffer_layer(cur, rules):
    """遍历全库所有 Bytes 列，应用 flatbuffer 变长替换。"""
    tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    changed = 0
    for table in tables:
        cols = [c[1] for c in cur.execute('PRAGMA table_info("%s")' % table)]
        if "Bytes" not in cols:
            continue
        pk = None
        for c in cur.execute('PRAGMA table_info("%s")' % table):
            if c[5]:
                pk = c[1]
                break
        sel = ('"%s"' % pk) if pk else "rowid"
        try:
            rows = cur.execute('SELECT %s, Bytes FROM "%s"' % (sel, table)).fetchall()
        except Exception:
            continue
        needles = [old.encode("utf-8") for old, _ in rules]
        for key, blob in rows:
            if not isinstance(blob, (bytes, bytearray)):
                continue
            data = bytes(blob)
            if not any(n in data for n in needles):
                continue
            new_blob, n = patch_flatbuffer(data, rules)
            if n:
                cur.execute(
                    'UPDATE "%s" SET Bytes=? WHERE %s=?' % (table, sel),
                    (sqlite3.Binary(new_blob), key))
                changed += 1
    return changed


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
        # 「等级」->「Lv.」，但整条跳过专有名词（羁绊等级 / 据点等级 …）。
        # 放在最后，让「好感等级 -> 羁绊等级」先定型，才不会被拆开误伤。
        # 「等级 -> Lv.」已按决定停用（LEVEL_WORD = None）。
        if LEVEL_WORD and LEVEL_WORD in value:
            if not any(p in value for p in LEVEL_PROTECT):
                value = value.replace(LEVEL_WORD, LEVEL_REPLACEMENT)
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

    # 形态后缀专项校验：全库（含名字层）不应再残留「（武装）」
    suffix_left = 0
    tabs = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    needle = "（武装）".encode("utf-8")
    for t in tabs:
        cols = [c[1] for c in con.execute('PRAGMA table_info("%s")' % t)]
        if "Bytes" not in cols:
            continue
        for (b,) in con.execute('SELECT Bytes FROM "%s"' % t):
            if isinstance(b, (bytes, bytearray)) and needle in bytes(b):
                suffix_left += 1

    con.close()

    # YEAR_RULES 已停用，不再替换「第N年」，因此只提示不中断。
    if sum(years.values()) < 200:
        print(f"[TEXT] 学年标签（YEAR_RULES 已停用）: {years}")

    if suffix_left:
        raise RuntimeError(
            f"形态后缀未清理干净：仍有 {suffix_left} 个 blob 含「（武装）」。"
            f"检查 NAME_RULES 中是否漏掉了非全角写法（如半角(武装)）。"
        )


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


    # flatbuffer 变长替换（提翡瑞/庚 等）


    fb_changed = apply_flatbuffer_layer(cur, FLATBUFFER_FLEX_RULES)


    print(f"[TEXT] 变长层      : {fb_changed} 行（flatbuffer）")

    # 变异层刚写完，立刻校验每个追加串都有 0x00 终止符。
    # 这一步是 2026-11-16 那次「卡正在初始化游戏数据」的防火墙：
    # 没有它，缺终止符的串会静默流到客户端才炸。
    bad = check_flex_terminators(cur)
    if bad:
        con.close()
        raise RuntimeError(
            "变长层校验未通过，共 %d 处，已中止（不会推送半成品）：\n%s"
            % (len(bad), "\n".join(
                "  %s rowid=%s %s  字段@%s 正文@%s  %r" % b for b in bad[:10]))
        )
    if fb_changed:
        print(f"[TEXT] 结构自检    : {fb_changed} 行，终止符/对齐/uoffset/长度 全部通过")

    con.commit()
    print(f"[TEXT] 名字层      : {name_changed} 行（全库）")

    year_changed = apply_year_layer(cur)
    con.commit()
    print(f"[TEXT] 学年层      : {year_changed} 个角色")

    # 名字改名已整体撤销：游戏里恢复官方原名「庚」。
    # 背景：变长方案（庚 3B -> 鹿江 6B）会让 10126 学生详情页卡死；
    # 等字节方案（庚 -> 鹿）能跑但名字只剩「板垣鹿」。两者都不理想，
    # 用户决定改回「庚」，所以这里既不调用 rename_geng，也不再自检
    # 「庚」是否清零 —— 它本来就该存在。
    _ = (rename_geng, apply_equal_len)  # 保留导入，供将来按需复用

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
