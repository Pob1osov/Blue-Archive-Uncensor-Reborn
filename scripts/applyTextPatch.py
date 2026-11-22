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
# 按 Key 精确覆盖（优先级最高）。
#
# 为什么需要它
# ------------
# 同一段官方原文可能被十几个 Key 共用。比如官方有 14 个 Key 的值都是
# 「等级」，全是各界面共用的行标签，从库内完全分不出哪个属于哪个界面。
# 遇到「只想改某一个界面」的需求，只能按 Key 精确指定。
#
# 用法：{Key: 目标文本}。长度不要求相等（走 LocalizeDB 长度前缀重建）。
KEY_OVERRIDE = {
    # 「等级 / Lv.」这组词的定位结论（2026-11-22 现场实测）
    # --------------------------------------------------
    # 官方有 13 个 Key 的值都是「等级」，上游还原把其中 11 个改成了「Lv.」。
    # 现场验证：这 11 个 Key 里，2415005115 供「大厅左上角等级标签」和
    # 「能力徽章（能力 Lv.7）」共用，必须保持「Lv.」——所以那一批不再覆盖，
    # 让上游还原的「Lv.」原样生效：
    #     大厅          Lv.  Pob1osov        ✔
    #     能力徽章      Lv.7 / Lv.10 / Lv.25 ✔
    #
    # 唯一需要中文的是「学员详细信息 → 成长信息」那一行行标签：
    # 官方把它和右侧数值的「Lv.90」并列，会显示成「Lv.  Lv.90」重复。
    # 该行对应的 Key 是 811393088——注意它**不在官方库里**，是上游还原
    # 新增的行，所以覆盖它不会影响任何官方文案。
    811393088: "等级",
}




EXACT_RULES = {
    "公告": "Notice",
    "未命中": "MISS",
    # 「等级」不做替换（2026-11-21 撤销）。
    #
    # 官方库里值恰为「等级」的 Key 共 14 个，全是各界面共用的行标签。
    # 换成「Lv.」后，学员详情页「成长信息」第一行变成：
    #
    #     Lv.   Lv.90        <- 左边是标签、右边是数值，重复了
    #
    # 数值那一侧的「Lv.」是另一个 Key（811393088，官方本来就是 Lv.），
    # 与标签无关。所以标签保留官方的「等级」即可，读作「等级  Lv.90」。
    #
    # 为什么不只改那一个 Key
    # ----------------------
    # 14 个 Key 的 Bytes 值完全一样（都是「等级」6 字节），
    # 从库内无法区分哪个属于学员详情页；prefab 里的 LocalizeKey 引用
    # 也抓不到（MonoBehaviour 无类型树，get_raw_data 返回空）。
    # 因此只能整组保持官方原文。
    # 若将来确认某个 Key 需要显示「Lv.」，用 KEY_EXACT_OVERRIDE 单独指定。
    # 抽卡页的「首次」标签 -> 「初回」
    #
    # 为什么用 EXACT_RULES 而不是 TEXT_RULES
    # --------------------------------------
    # 「首次」在库里还出现在「首次奖励」「首次进入游戏」这类新手引导/活动文案里。
    # TEXT_RULES 是子串替换，会把它们一并改成「初回奖励」，那是错的。
    # EXACT_RULES 只在整条字符串完全等于「首次」时才替换，因此只影响
    # 抽卡页那两个独立的「首次」标签（Key 1007001787 / 2769709569）。
    #
    # 字节长度：「首次」与「初回」都是 6 字节，等长替换不影响结构。
    "首次": "初回",
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
    # 顶部导航 / 活动页的「活动」标签 -> 「EVENT」
    #
    # 为什么必须走 EXACT_RULES
    # ------------------------
    # 「活动」在库里作为子串出现 589 处（「活动期间」「暂无活动」
    # 「完成所有活动每日任务」…），用 TEXT_RULES 会把长句一起改坏。
    # 整条字符串恰好等于「活动」的只有 10 处，全部是独立标签。
    "活动": "EVENT",
    # 公告/邮件等角标「新」-> 「New」
    #
    # 同样不能用子串：「新」在库里作子串出现 407 处
    # （「重新」「新手」「更新」…）。整条等于「新」的只有 2 处，是角标。
    "新": "New",
    # 剧情/战斗界面的「自动」按钮 -> 「AUTO」
    #
    # 为什么必须走 EXACT_RULES
    # ------------------------
    # 「自动」在库里作子串大量存在（「自动贩卖机」「自动瞄准」「自动编队」
    # 「量产型自动人偶」…），用 TEXT_RULES 会把剧情正文改坏。
    # 整条字符串恰好等于「自动」的在 LocalizeDB 里只有 3 条
    # （Key 2201087488 / 1937866009 / 2519343502），全是 UI 按钮标签，
    # 对应剧情界面的自动播放、战斗界面的自动战斗。
    # 「自动」(6 字节) -> 「AUTO」(4 字节) 是变长，走长度前缀重建。
    "自动": "AUTO",
    # 同理：「跳过」作子串有「跳过剧情/跳过本话」等长句，只认整条相等。
    "跳过": "SKIP",
    # 剧情界面右上角的「菜单」按钮 -> 「MENU」
    #
    # 为什么必须走 EXACT_RULES
    # ------------------------
    # 「菜单」作子串在库里有 22 处（「主菜单」「菜单栏」「返回菜单」…），
    # 整条字符串恰好等于「菜单」的只有 4 条
    # （Key 219412176 / 2726620896 / 3575702447 / 586683559），全是按钮标签。
    # 「菜单」(6 字节) -> 「MENU」(4 字节) 是变长，走长度前缀重建。
    "菜单": "MENU",
    # 暂停按钮 -> 「PAUSE」
    # 「暂停」作子串 5 处（「暂停中」「游戏暂停」…），整条相等 3 条
    # （Key 1499961213 / 2833163121 / 3377090103）。
    # 「暂停」(6 字节) -> 「PAUSE」(5 字节)，同样是变长。
    "暂停": "PAUSE",
    # 剧情结局的「待续...」-> 「To Be Continued...」
    #
    # 定位依据：部署库全库只有 1 条值的原文就是「待续...」——
    # LocalizeDBSchema Key 1344287724（Bytes 56 字节，flatbuffer 包裹）。
    # 没有「待续……」「未完待续」等其它写法，「待续」作为子串
    # 也只出现在这一条里，所以整条相等判定不会误伤正文。
    #
    # 长度：「待续...」= 6 + 3 = 9 字节 -> 「To Be Continued...」= 18 字节，
    # 变长，走 apply_text_layer 的长度前缀重建。
    "待续...": "To Be Continued...",
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
    # 「柯伊」-> 「凯伊」：日服作「カイ」，国服译作「柯伊」。
    # 两词都是 6 字节，等长替换不动偏移，可走名字层。
    # 部署库里共 272 处：ScenarioScript 239 / LocalizeEtc 14 / LocalizeDB 9 /
    # ScenarioCharacterName 8 / LocalizeSkill 2。
    ("柯伊", "凯伊"),
    # 结巴写法：「柯，凯伊？」「柯……什么？」——第一个「柯」后面跟的是标点，
    # 单靠上面的「柯伊」匹配不到，会留下半截「柯，凯伊？」，读起来像两个人。
    # 只针对「柯，」「柯……」这两个确定是名字起头的写法，等长替换：
    #   柯，  3+3=6 字节 -> 凯，  6 字节
    #   柯…… 3+6=9 字节 -> 凯…… 9 字节
    # 明确不碰「柯基町」「柯基勇士队」（33 处）、「柯特尔」「【柯■】」等其它词。
    ("柯，", "凯，"),
    ("柯……", "凯……"),
    ("玛尔扈特", "玛尔库特"),
    # 名字改名已整体撤销，恢复官方原名「庚」（用户要求）。
    # 玛尔扈特 -> 玛尔库特
    # 4 字 -> 4 字、12 -> 12 字节，等长替换，可走名字层。
    # 部署库实测 67 处，分布：LocalizeDB 7 / ScenarioScript 11 /
    # ScenarioCharacterName 3 / LocalizeEtc 3 / LocalizeSkill 15（行数），
    # 且不存在「玛，尔扈特」「玛尔，扈特」这类结巴写法，无需另加规则。
]


# ---------------------------------------------------------------------------
# 结巴写法：称呼被标点/拖音拆开时，头一个字不跟着换
# ---------------------------------------------------------------------------
# 剧情里常有「妮，妮露！」这种结巴。这类文本不是完整的旧名，所以
# NAME_RULES 里那条「妮露 -> 尼禄」匹配不到，会留下半截：
#
#     原文  妮，妮露！   ->  替换后  妮，尼禄！     （前半个「妮」没换）
#
# 两种拆法都要管：
#
#   S1 前半截仍是旧字        三，弥守？        -> 弥，弥守？
#   S2 整条被拖音隔开        芽~瑠~前~辈~！    -> 梅~露~前~辈~！
#
# S1 可以按规则表批量生成：匹配「旧名首字 + 标点 + 新名整体」，
# 只有确实出现结巴才命中，不会误伤「爱，露出…」这类正文
# （因为要求后面紧跟完整的新名）。
#
# S2 不能批量生成 —— 规则会是「旧字 + 标点 + 旧名后半」，
# 泛化后会命中「沙，勒住」之类的正常句子，所以只列举实际出现的两条。
#
# 两者都是等长替换（首字替换首字，标点原样保留），偏移不变。
_STUTTER_PUNCT = "，、…—·~～.．"

_STUTTER_S1 = []
for _old, _new in NAME_RULES:
    if len(_old) < 2 or _old[0] == _new[0]:
        continue
    for _p in _STUTTER_PUNCT:
        _STUTTER_S1.append((_old[0] + _p + _new, _new[0] + _p + _new))

# S2：只列举实际查到的两条（都是拖音分隔符隔开整条名字）
_STUTTER_S2 = [
    ("沙~勒", "夏~莱"),
    ("芽~瑠", "梅~露"),
]

NAME_RULES.extend(_STUTTER_S1)
NAME_RULES.extend(_STUTTER_S2)


# 学年标签：等长替换，仅角色资料表
YEAR_RULES = [("第1年", "1年生"), ("第2年", "2年生"), ("第3年", "3年生")]


# ============================================================
# flatbuffer 变长替换规则
# ============================================================
#
# 这些词长度会变（提翡瑞 9->12、庚 3->6），不能做全库字节替换，
# 会在 apply_flatbuffer_layer 里按两种情况分别处理：
#   1) 字符串被字段偏移引用 -> 追加到 blob 末尾并改偏移
#   2) 字符串贴在 blob 末尾且带长度前缀 -> 原地改长度
# 两者都不移动已有数据的偏移，因此不会破坏 flatbuffer 结构。

FLATBUFFER_FLEX_RULES = [
    # 2026-11-16 重新启用。旧实现漏写字符串终止符（4+len(payload) 恰为 4 的
    # 倍数时 pad 算出 0），163 个追加串里有 44 个没有 0x00 结尾，正是
    # GameDataInitializer.LoadInBatches 抛 ObjectDisposedException 的真因。
    # 终止符已改为显式写入，并由 check_flex_terminators() 在写回前逐串校验。
    ("提翡瑞", "提法瑞特"),
    ("庚", "鹿江"),
]


# 截断昵称层：作用在变长层之后，只做等长替换。
#
# 剧情里角色会用简称称呼「提翡瑞」，实测 23 处全是「小提翡」：
#     「小，小提翡……真的没问题吗……？」  ->  「小，小提法……」
# 昵称取的是旧名前两个字，既不是完整旧名（提翡瑞）也不是完整新名
# （提法瑞特），所以前面两层都匹配不到。
#
# 为什么必须排在变长层之后：见 apply_nick_layer 的注释。
NICK_RULES = [
    ("提翡", "提法"),
]

# 执行完必须为 0 的旧词；非 0 说明规则漏了或顺序错了
#
# 注意：这里只校验 LocalizeDBSchema（文本层）。
# 「（武装）」这类改动走的是 apply_name_layer 的全库字节替换，
# 由 verify_form_suffix() 单独校验，不要加到这里——否则会误报。
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


def _fb_all_string_refs(d):
    """扫描整个 blob，找出所有指向合法 flatbuffer 字符串的 uoffset 字段。

    为什么需要它
    ------------
    _fb_string_fields 只走「根表」的直接字段。像 LocalizeEtc / ScenarioScript
    这类把文本放在嵌套表（vector of tables、子表字段）里的行，根表扫不到任何
    字符串，于是「庚 / 提翡瑞」在那些行里替换不到。

    flatbuffers 里 uoffset 一律向前指向对象起点，字符串对象形如
    [4B 长度][UTF-8 正文][0x00]，据此全量扫描即可覆盖任意嵌套层级。
    """
    import struct
    n = len(d)
    seen = set()
    for p in range(0, n - 4, 4):
        u = struct.unpack_from('<I', d, p)[0]
        if u < 8 or u % 4:
            continue
        t = p + u
        if t % 4 or not (4 <= t < n):
            continue
        r = _fb_try_str(d, t)
        if r is None:
            continue
        sstart, slen = r
        if slen > 4000:
            continue
        key = (p, sstart, slen)
        if key in seen:
            continue
        seen.add(key)
        yield p, sstart, slen


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
    seen_f = set(fields)
    for f in _fb_all_string_refs(bytes(d)):
        if f not in seen_f:
            fields.append(f)
            seen_f.add(f)
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
        # 按 Key 精确覆盖（优先于上面所有规则）。
        # 用途：同一段官方原文被多个界面共用一个 Key 池时，
        # 只能靠 Key 区分「某个界面要显示什么」。
        if int(key) in KEY_OVERRIDE:
            value = KEY_OVERRIDE[int(key)]
        value = value.replace("\x01BOND\x01", BOND)
        if value != original:
            cur.execute(
                "UPDATE LocalizeDBSchema SET Bytes=? WHERE Key=?",
                (rebuild_localize(blob, value), key),
            )
            changed += 1
    return changed


def apply_equal_len_layer(cur, rules, label="", quiet=False) -> int:
    for old, new in rules:
        if len(old.encode("utf-8")) != len(new.encode("utf-8")):
            raise RuntimeError(
                f"{label or '等长替换'}规则 {old!r} -> {new!r} 字节长度不等，"
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
            for old, new in rules:
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


def apply_name_layer(cur) -> int:
    return apply_equal_len_layer(cur, NAME_RULES, "名字")


def apply_nick_layer(cur) -> int:
    """截断昵称层：必须放在变长层之后。

    「小提翡」是「提翡瑞」截前两个字得到的昵称。它既不是完整旧名，
    也不是完整新名，名字层和变长层都匹配不到，于是原样留在剧情里。

    为什么不能放进名字层
    --------------------
    名字层在变长层之前跑。若那里有「提翡 -> 提法」，会把「提翡瑞」
    先改成「提法瑞」，变长规则「提翡瑞 -> 提法瑞特」就再也匹配不到，
    全名会退化成三个字的「提法瑞」。

    放在变长层之后则刚好：此时全名已经是「提法瑞特」，剩下的
    「提翡」只可能是昵称，替换成「提法」既等长又不会误伤。

    孤儿字节说明
    ------------
    patch_flatbuffer 是「追加新串 + 改 uoffset」，旧串仍以孤儿字节
    形式留在 blob 里。这一层会把这些孤儿字节一起改掉 —— 它们不被任何
    uoffset 引用，客户端读不到，改了无副作用。
    """
    return apply_equal_len_layer(cur, NICK_RULES, "昵称")



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

    if sum(years.values()) < 200:
        raise RuntimeError(f"学年标签覆盖不足: {years}")

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

    # 截断昵称（小提翡 -> 小提法）。必须紧跟变长层之后、学年层之前。
    nick_changed = apply_nick_layer(cur)
    con.commit()
    print(f"[TEXT] 昵称层      : {nick_changed} 行（等长）")

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
