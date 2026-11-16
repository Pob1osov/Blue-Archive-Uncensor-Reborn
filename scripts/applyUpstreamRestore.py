# -*- coding: utf-8 -*-
"""整行还原上游已手工改好的行（8 张表、448 个主键）。

上游的做法是「用改好的旧库按主键整行覆盖」，其旧库里已经完成了
等级→Lv.、评级→RANK、桃信→MomoTalk、桃桃朋友→MomoFriends、
地下市场→黑市、快速战斗→扫荡、成员→学员、最小值/最大值→MIN/MAX 等替换。
所以这里直接全量覆盖，不做任何「缺词」判断 —— 那是误判：
中文词并不是被删掉，而是被换成了日服说法。
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def load_config() -> dict:
    p = Path(__file__).resolve().parent / "upstream_restore_ids.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    return {t: (c["primary_key"], c["ids"]) for t, c in raw.items()}


def restore(old_db: Path, new_db: Path, verbose: bool = True) -> tuple:
    """返回 (还原行数, 缺失行数)。"""
    cfg = load_config()
    con_old = sqlite3.connect(str(old_db))
    con_new = sqlite3.connect(str(new_db))
    cur_old = con_old.cursor()
    cur_new = con_new.cursor()

    total = missing = 0
    cur_new.execute("BEGIN TRANSACTION;")

    for table, (pk, ids) in cfg.items():
        if not ids:
            continue
        hit = miss = 0
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
                miss += 1
                continue

            for (orow, ob), (nrow, nb) in zip(old_rows, new_rows):
                if not isinstance(ob, (bytes, bytearray)):
                    miss += 1
                    continue
                cur_new.execute(
                    f'UPDATE "{table}" SET Bytes=? WHERE rowid=?', (bytes(ob), nrow))
                hit += 1

        total += hit
        missing += miss
        if verbose:
            print(f"  {table:<34} 还原 {hit:>6} 行"
                  + (f", 缺失 {miss} 个 ID" if miss else ""))

    cur_new.execute("COMMIT;")
    con_old.close()
    con_new.close()
    return total, missing


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
    n, m = restore(old_db, new_db)
    print(f"[RESTORE] 合计还原 {n} 行，{m} 个 ID 未命中")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
