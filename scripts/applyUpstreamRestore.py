# -*- coding: utf-8 -*-
"""保护性还原：整行还原，但如果会「丢掉词」导致缺词，则跳过该行。

判定：官方行里含某个受保护词，而旧库同一行不含 —— 直接用旧行就会缺词。
这类行改为保留官方原文（不缺词），再由文本层补上日服词。
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 受保护词 -> 日服词（文本层会把它们换成日服说法）
PROTECT = {
    "桃信": "MomoTalk",
    "桃桃朋友": "MomoFriends",
    "地下市场": "黑市",
    "快速战斗": "扫荡",
    "最小值": "MIN",
    "最大值": "MAX",
    "评级": "Rank.",
}


def load_config() -> dict:
    p = Path(__file__).resolve().parent / "upstream_restore_ids.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    return {t: (c["primary_key"], c["ids"]) for t, c in raw.items()}


def restore(old_db: Path, new_db: Path, verbose: bool = True) -> tuple:
    """返回 (还原行数, 跳过的缺词行数)。"""
    cfg = load_config()
    con_old = sqlite3.connect(str(old_db))
    con_new = sqlite3.connect(str(new_db))
    cur_old = con_old.cursor()
    cur_new = con_new.cursor()

    protect_b = {w.encode("utf-8") for w in PROTECT}
    total = skipped = 0

    cur_new.execute("BEGIN TRANSACTION;")
    for table, (pk, ids) in cfg.items():
        if not ids:
            continue
        hit = skip = 0
        for tid in ids:
            cur_old.execute(
                f'SELECT rowid, Bytes FROM "{table}" WHERE "{pk}" = ? ORDER BY rowid',
                (tid,))
            old_rows = cur_old.fetchall()
            cur_new.execute(
                f'SELECT rowid, Bytes FROM "{table}" WHERE "{pk}" = ? ORDER BY rowid',
                (tid,))
            new_rows = cur_new.fetchall()

            if not old_rows or not new_rows or len(old_rows) != len(new_rows):
                continue

            for (orow, ob), (nrow, nb) in zip(old_rows, new_rows):
                if not isinstance(ob, (bytes, bytearray)):
                    continue
                ob = bytes(ob)
                nb = bytes(nb) if isinstance(nb, (bytes, bytearray)) else b""

                # 官方有、旧库没有 => 会缺词，跳过
                lost = any(w in nb and w not in ob for w in protect_b)
                if lost:
                    skip += 1
                    continue

                cur_new.execute(
                    f'UPDATE "{table}" SET Bytes=? WHERE rowid=?', (ob, nrow))
                hit += 1
        total += hit
        skipped += skip
        if verbose:
            print(f"  {table:<34} 还原 {hit:>6} 行, 跳过缺词 {skip:>4} 行")

    con_new.commit()
    con_old.close()
    con_new.close()
    return total, skipped


def main() -> int:
    if len(sys.argv) < 3:
        print("用法: applyUpstreamRestore.py <ExcelDB_old.db> <ExcelDB_new.db>")
        return 2
    old_db, new_db = Path(sys.argv[1]), Path(sys.argv[2])
    for p in (old_db, new_db):
        if not p.exists():
            print(f"[RESTORE] 找不到 {p}")
            return 2
    print(f"[RESTORE] 旧库 {old_db.name} ({old_db.stat().st_size} B)")
    print(f"[RESTORE] 新库 {new_db.name} ({new_db.stat().st_size} B)")
    n, s = restore(old_db, new_db)
    print(f"[RESTORE] 合计还原 {n} 行，为防缺词跳过 {s} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
