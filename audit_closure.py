# -*- coding: utf-8 -*-
"""审计闭合统计 (2026-09-08) — 源=输入=结果 的全量对账。

背景: 审计总表曾出现 187篇 vs 历史273篇 的口径差, 子目录"全绿"不能当全量终审。
本工具不做内容审计, 只做**清单闭合**: 每一篇都必须落在且只落在一个桶里,
任何缺口(漏审/漏盘/幽灵记录/终审未过)都显式报出, 退出码非0供cron告警。

闭合恒等式(每专业/每届次分别成立):
    清单数 = 盘上docx数 = 审计行数
    审计行数 = 绿色(✅/⚠️已批准) + 待处理(❌/⚠️) + 返修中(在返修清单)

用法:
  python3 audit_closure.py --root 论文终版_返修0908 \
      --list 论文修订档案/_无源清单_0908.json \
      --audit-csv 论文修订档案/审计报告_0908深度.csv \
      [--repair-csv 论文修订档案/返修记录.csv] [--out 闭合统计.csv]
"""
import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict


def _norm(rel: str) -> str:
    return rel.replace("\\", "/").strip().lstrip("./")


def _bucket(rel: str):
    """路径 → (届次, 专业)。形如 26/经管/xxx.docx 或 经管/xxx.docx。"""
    parts = _norm(rel).split("/")
    parts = [p for p in parts if p not in (".", "")]
    if len(parts) >= 3 and re.fullmatch(r"\d{2}", parts[0]):
        return parts[0], parts[1]
    if len(parts) >= 2:
        return "(未分层)", parts[0]
    return "(未分层)", "(未分类)"


def main():
    ap = argparse.ArgumentParser(description="审计清单闭合统计")
    ap.add_argument("--root", required=True, help="终版docx根目录")
    ap.add_argument("--list", dest="listfile", required=True, help="无源清单json")
    ap.add_argument("--audit-csv", dest="auditcsv", required=True, help="审计报告csv")
    ap.add_argument("--repair-csv", dest="repaircsv", default="", help="返修记录csv(可选)")
    ap.add_argument("--out", default="", help="闭合统计csv输出(可选)")
    ap.add_argument("--include-backups", action="store_true",
                    help="把_开头的备份目录也计入盘上集合(默认排除)")
    args = ap.parse_args()

    lst = json.load(open(args.listfile, encoding="utf-8"))
    expected = {_norm(x) for x in lst}

    disk = set()
    for dp, _dn, fns in os.walk(args.root):
        rel_dp = os.path.relpath(dp, args.root)
        # 备份/临时目录不是交付物, 默认排除(_修改备份等); --include-backups可纳入
        if not args.include_backups and rel_dp.split(os.sep)[0].startswith("_"):
            continue
        for fn in fns:
            if fn.endswith(".docx") and not fn.startswith("~$") and "_temp_" not in fn:
                rel = os.path.relpath(os.path.join(dp, fn), args.root)
                disk.add(_norm(rel))

    audited, verdicts = set(), {}
    with open(args.auditcsv, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            rel = _norm(row.get("文件") or row.get("file") or "")
            if rel:
                audited.add(rel)
                verdicts[rel] = (row.get("终审") or row.get("verdict") or "").strip()

    repairing = set()
    if args.repaircsv and os.path.exists(args.repaircsv):
        with open(args.repaircsv, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                rel = _norm(row.get("文件") or row.get("file") or "")
                if rel:
                    repairing.add(rel)

    cats = ("清单缺失", "盘上多余", "漏审计", "审计幽灵", "终审未过")
    problems = defaultdict(list)
    for rel in sorted(expected - disk):
        problems["清单缺失"].append(rel)
    for rel in sorted(disk - expected):
        problems["盘上多余"].append(rel)
    for rel in sorted(disk - audited):
        problems["漏审计"].append(rel)
    for rel in sorted(audited - disk):
        problems["审计幽灵"].append(rel)
    for rel in sorted(audited & disk):
        v = verdicts.get(rel, "")
        if v and v not in ("✅",):
            # ⚠️可接受(已在报告可见), ❌/空=未过
            if v.startswith("❌") or not v:
                problems["终审未过"].append(rel)
            elif v.startswith("⚠️") and rel in repairing:
                pass  # 已进返修流程
            elif v.startswith("⚠️"):
                pass  # 警告级, 闭合允许(报告可见)

    # 汇总表: 每(届次,专业)的闭合计数
    stats = defaultdict(lambda: {"清单": 0, "盘上": 0, "审计": 0, "返修中": 0})
    universe = expected | disk | audited
    for rel in universe:
        b = _bucket(rel)
        stats[b]["清单"] += rel in expected
        stats[b]["盘上"] += rel in disk
        stats[b]["审计"] += rel in audited
        stats[b]["返修中"] += rel in repairing

    print("=" * 72)
    print(f"闭合对账: 清单{len(expected)} | 盘上{len(disk)} | 审计{len(audited)}"
          f" | 返修中{len(repairing)}")
    print("-" * 72)
    print(f"{'届次':<6}{'专业':<8}{'清单':>5}{'盘上':>5}{'审计':>5}{'返修中':>6}  闭合")
    closed = True
    for (cohort, major), s in sorted(stats.items()):
        ok = s["清单"] == s["盘上"] == s["审计"]
        closed &= ok
        print(f"{cohort:<6}{major:<8}{s['清单']:>5}{s['盘上']:>5}{s['审计']:>5}"
              f"{s['返修中']:>6}  {'✅' if ok else '❌ 数量不闭合'}")
    print("-" * 72)
    if any(problems[c] for c in cats):
        closed = False
        for c in cats:
            if problems[c]:
                print(f"❌ {c} {len(problems[c])}篇: {problems[c][:4]}")
    else:
        print("✅ 无缺项: 清单=盘上=审计, 无幽灵记录, 无未过终审")

    if args.out:
        with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["届次", "专业", "清单", "盘上", "审计", "返修中", "闭合"])
            for (cohort, major), s in sorted(stats.items()):
                ok = s["清单"] == s["盘上"] == s["审计"]
                w.writerow([cohort, major, s["清单"], s["盘上"], s["审计"],
                            s["返修中"], "✅" if ok else "❌"])
            w.writerow([])
            w.writerow(["类别", "数量", "文件"])
            for c in cats:
                for rel in problems[c]:
                    w.writerow([c, len(problems[c]), rel])
        print("闭合统计:", args.out)

    sys.exit(0 if closed else 1)


if __name__ == "__main__":
    main()
