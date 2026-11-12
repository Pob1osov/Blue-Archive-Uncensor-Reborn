#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
renameGeng.py —— 全库把「庚」改名成「鹿江」，保证 blob 结构不被破坏

槽位识别
--------
不用「从头顺序扫描」。真实 blob 里存在大量非字符串段，顺序扫描一旦
错位就会把垃圾字节当成一个大字符串。这里改成：对每一处「庚」，从它
往前最多 512 字节里找一个合法的长度前缀 q，使得
    q + 4 + L 覆盖这处「庚」且该段能按 UTF-8 解出可打印文本。
取最近的那个 q —— 这正好是包含它的那个字符槽。

三种安全改法（都保证「行内其它字符串的位置完全不动」）
--------------------------------------------------
S. 缩小（new 比 old 短）：写回短文本，余量补零，行总长不变。
A. 放大且槽后有足够零填充：吃掉同等数量的零字节，行总长不变。
B. 放大且槽在行末（槽后到行尾全是零）：直接变长，后面没有内容会错位。
其余情况交给 C 表（等字节重写），再不行就跳过并报告，绝不硬改。
"""

from __future__ import annotations

import sqlite3

OLD = "庚"
NEW = "鹿江"
OLD_B = OLD.encode()
NEW_B = NEW.encode()

MAX_BACK = 512

# LocalizeDBSchema 不是 flatbuffer，而是固定布局：
#   [40 字节头][4 字节长度][UTF-8 文本][补齐到 4 的倍数（整除时也补 4）]
# 它的填充规则由长度推导，不能「吃掉尾部零字节」来吸收变长，
# 否则 44 + L + pad_for(L) 就不再等于行长度，客户端解析直接失败。
# 这张表必须走 autopatch 里的文本层重建路径，这里一律跳过。
SKIP_TABLES = {"LocalizeDBSchema"}


def find_slot(data: bytes, i: int, needle_len: int):
    """找包含 data[i:i+needle_len] 的长度前缀槽，返回 (q, L, text) 或 None。"""
    best = None
    for back in range(4, MAX_BACK):
        q = i - back
        if q < 0:
            break
        L = int.from_bytes(data[q:q + 4], "little")
        if L <= 0 or L > 8192 or q + 4 + L > len(data):
            continue
        if not (q + 4 <= i and i + needle_len <= q + 4 + L):
            continue
        try:
            s = data[q + 4:q + 4 + L].decode("utf-8")
        except Exception:
            continue
        if s.isprintable() or "\n" in s:
            best = (q, L, s)
            break
    return best


def _zero_run(d, j):
    z = 0
    while j + z < len(d) and d[j + z] == 0:
        z += 1
    return z


def rewrite_slot(d: bytearray, q: int, L: int, s: str, old: str, new: str):
    """在槽内改名。成功返回 'S'/'A'/'B'，失败 None。"""
    if old not in s:
        return None
    ns = s.replace(old, new)
    nb = ns.encode()
    delta = len(nb) - L
    j = q + 4 + L

    if delta <= 0:
        # S：缩小，尾部补零填满原位，行总长不变
        d[q:q + 4] = len(nb).to_bytes(4, "little")
        d[q + 4:q + 4 + len(nb)] = nb
        d[q + 4 + len(nb):j] = b"\x00" * (-delta)
        return "S"

    z = _zero_run(d, j)
    if z >= delta:
        # A：吃零填充
        d[q:q + 4] = len(nb).to_bytes(4, "little")
        d[q + 4:j] = nb
        del d[j:j + delta]
        return "A"

    if j + z >= len(d):
        # B：行末槽，直接变长
        d[q:q + 4] = len(nb).to_bytes(4, "little")
        d[q + 4:j] = nb
        return "B"

    return None


def _process_blob(d: bytearray):
    """反复处理，直到没有可改的「庚」。返回 (S数, A数, B数, 残留文本列表)"""
    counts = {"S": 0, "A": 0, "B": 0}
    for _ in range(1000):
        data = bytes(d)
        idx = data.find(OLD_B)
        progressed = False
        pos = 0
        while idx >= 0:
            slot = find_slot(data, idx, len(OLD_B))
            if slot:
                q, L, s = slot
                if OLD in s:
                    kind = rewrite_slot(d, q, L, s, OLD, NEW)
                    if kind:
                        counts[kind] += 1
                        progressed = True
                        break
            pos = idx + len(OLD_B)
            idx = data.find(OLD_B, pos)
        if not progressed:
            break

    left = []
    data = bytes(d)
    pos = 0
    while True:
        idx = data.find(OLD_B, pos)
        if idx < 0:
            break
        slot = find_slot(data, idx, len(OLD_B))
        if slot and OLD in slot[2]:
            left.append(slot[2])
        pos = idx + len(OLD_B)
    return counts["S"], counts["A"], counts["B"], left


def _iter_rows(cur):
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
        yield t, sel, cur.execute('SELECT %s, Bytes FROM "%s"' % (sel, t)).fetchall()


def rename_geng(cur, dry=False):
    """返回 (改动行数, 各路径计数 dict, 残留[(表, 键, 文本)])"""
    rows = 0
    total = {"S": 0, "A": 0, "B": 0}
    left = []

    for t, sel, allrows in _iter_rows(cur):
        for key, blob in allrows:
            if not isinstance(blob, (bytes, bytearray)):
                continue
            d = bytearray(blob)
            if OLD_B not in bytes(d):
                continue
            s_n, a_n, b_n, rem = _process_blob(d)
            if s_n or a_n or b_n:
                total["S"] += s_n
                total["A"] += a_n
                total["B"] += b_n
                rows += 1
                if not dry:
                    cur.execute('UPDATE "%s" SET Bytes=? WHERE %s=?' % (t, sel),
                                (sqlite3.Binary(bytes(d)), key))
            for s in rem:
                left.append((t, key, s))

    return rows, total, left


# ------------------------------------------------------------------
# C 路径：等字节重写。用于「放大但既无零填充、又不在行末」的紧凑槽。
# 必须逐条校验字节数相等。
# ------------------------------------------------------------------
EQUAL_LEN_RULES = [
    ("啊啊，老师！庚~真的好开心啊！\n……这怎么样？嘻嘻。",
     "啊啊，老师！鹿江~真的好开心啊！\n……这怎么样？嘻。"),
    ("庚制作的热巧克力。看起来有些诡异，但味道非常不错。",
     "鹿江制作的热巧克力。看起来有些诡异，味道非常不错。"),
    ("庚惯用的霰弹枪。\n\n据称是……通过炼金术引起的化学反应来发射弹药。",
     "鹿江惯用的霰弹枪。\n\n据称是……以炼金术引起的化学反应来发射弹药。"),
    ("庚的神名文字，蕴含着庚的神秘。",
     "鹿江的神名文字，蕴含鹿江的神秘"),
]


def check_equal_len():
    bad = [(o, n, len(o.encode()), len(n.encode()))
           for o, n in EQUAL_LEN_RULES if len(o.encode()) != len(n.encode())]
    if bad:
        raise RuntimeError("C 规则字节数不等，会破坏偏移: %s" % bad)


def apply_equal_len(cur):
    """按整槽文本精确相等应用 C 路径。返回命中数。"""
    check_equal_len()
    hits = 0
    for t, sel, allrows in _iter_rows(cur):
        for key, blob in allrows:
            if not isinstance(blob, (bytes, bytearray)):
                continue
            d = bytearray(blob)
            changed = False
            data = bytes(d)
            pos = 0
            while True:
                idx = data.find(OLD_B, pos)
                if idx < 0:
                    break
                slot = find_slot(data, idx, len(OLD_B))
                if slot:
                    q, L, s = slot
                    for old, new in EQUAL_LEN_RULES:
                        if s == old:
                            d[q + 4:q + 4 + L] = new.encode()   # 等长，前缀不动
                            changed = True
                            hits += 1
                            data = bytes(d)
                            break
                pos = idx + len(OLD_B)
            if changed:
                cur.execute('UPDATE "%s" SET Bytes=? WHERE %s=?' % (t, sel),
                            (sqlite3.Binary(bytes(d)), key))
    return hits
