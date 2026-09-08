# -*- coding: utf-8 -*-
"""
audit_txt.py — txt 产物预渲染审计层（零LLM，确定性）
====================================================
生成环节产出 txt 后、进入 renderer 前，先过本审计层。
设计原则：审计与修复分离——本层只报告不改写（改写是 tagguard 的职责），
避免"检查即副作用"（见护栏加固方法论教训）。

维度：
  T1 标签变体: <graphic>/<figure>/<image>/<fig> 等非标准标签（渲染器不认→图静默丢失）
  T2 LaTeX残留: \frac|\geq|\approx|\times|\\[|\\(|\text{ 等公式源码
  T3 裸行图注: "5-1 基础施工阶段平面布置图" 式无图字头行
  T4 裸标题+括号: "路面结构层示意图"+下一行"(…图中标注…)" 模式
  T5 孤儿表注: "表N-N 标题"行后至下一标题无 <table 标签
  T6 孤儿图注: "图N-N 标题"行后无 drawing/chart 标签
  T7 摘要区残留: 摘要→关键词 之间出现任何结构化标签
  T8 引用悬空: 正文"图N-M/表N-M"引用 ∉ 图注意图集合

判级：hard=阻断渲染（必须修）; warn=告警（记录后可继续）
用法:
    python3 audit_txt.py <txt路径>...        # 单篇/多篇
    python3 audit_txt.py --dir 目录           # 批量
    python3 audit_txt.py --dir 目录 --csv out.csv
"""
import os
import re
import sys
import csv
import glob
import argparse

# ---------- 模式 ----------
RE_ALIAS_TAG = re.compile(r"<(?:graphic|figure|fig|image)\b")  # 标签头即报: 变体常缺右尖括号>
RE_LATEX = re.compile(r"\\frac|\\geq|\\leq|\\approx|\\times|\\sum|\\cdot|\\sqrt|\\\[|\\\(|\\text\{|\\mathrm\{")
RE_BARE_CAP = re.compile(r"^\d{1,2}-\d{1,3}\s+\S.{3,38}$", re.M)
RE_BARE_TITLE = re.compile(r"^[\u4e00-\u9fa5]{2,14}(?:示意图|布置图|剖面图|大样图|流程图|横道图)$", re.M)
RE_PAREN_DESC = re.compile(r"^\(.*图中", re.M)
RE_TCAP = re.compile(r"^表\s?\d{1,2}[-–]\d{1,3}\s+\S", re.M)
RE_FCAP = re.compile(r"^图\s?\d{1,2}[-–]\d{1,3}\s+\S", re.M)
RE_REF = re.compile(r"(?<![简插附纸样意标流路线效])图\s?(\d{1,2}[-–]\d{1,3})")
RE_TREF = re.compile(r"(?<![数逐附])表\s?(\d{1,2}[-–]\d{1,3})")
RE_HEADING = re.compile(r"^(?:第\s*\d+\s*章|\d+\.\d+(\.\d+)?\s)", re.M)
# T9 无编号指代: 如图/下图/上图/图中/见图/图示 (无编号的图指称)
RE_UNNUM_REF = re.compile(r"如图所示|如下图|如上图|见下图|见图|图[中示]|本图|该图|[，。；]下图|[，。；]上图")
# 带编号指代归T8对账域,不算空承诺
RE_NUMED_REF = re.compile(r"图\s?\d{1,2}[-–]\d{1,3}")
RE_NOT_REF = re.compile(r"试图|意图|版图|宏图|蓝图|图纸|图形|地图书|图书|企图")
RE_ANCHOR = re.compile(r"<(?:drawing|chart|table|graphic)\b|^图\s?\d{1,2}[-–]\d{1,3}\s+\S|^表\s?\d{1,2}[-–]\d{1,3}\s+\S|^[\u4e00-\u9fa5]{2,14}(?:示意图|布置图|剖面图|大样图|流程图|横道图)$", re.M)
RE_SOFTEN = re.compile(r"(?:类似|参照|其他工程|其他项目|文献|规范中?|教材)[^。]{0,12}$")


def audit_txt(text: str) -> dict:
    """审计一篇 txt 正文，返回 {级别: [问题...]}。只报告不改写。"""
    hard, warn = [], []

    # 自报清单块是审计对账材料，不是正文图注/锚点；先留存原文供T10对账，再剥除做正文级检查。
    mrep = re.search(r"\[FIGURES\]\s*\n(.*?)\[/FIGURES\]", text, re.S)
    text = re.sub(r"\[FIGURES\]\s*.*?\[/FIGURES\]\s*", "", text, flags=re.S)

    # T1 标签变体
    aliases = RE_ALIAS_TAG.findall(text)
    if aliases:
        hard.append(f"T1标签变体×{len(aliases)}: {aliases[0][:44]}…")

    # T2 LaTeX 残留
    lat = RE_LATEX.findall(text)
    if lat:
        hard.append(f"T2 LaTeX残留×{len(lat)}: 首见 {lat[0]!r}")

    # T3 裸行图注（排除目录行=尾部页码）
    bares = [l for l in RE_BARE_CAP.findall(text) if not re.search(r"\d\s*$", l)]
    if bares:
        hard.append(f"T3裸行图注×{len(bares)}: {bares[0][:30]}")

    # T4 裸标题+括号参数
    lines = text.splitlines()
    for i in range(len(lines) - 1):
        if RE_BARE_TITLE.match(lines[i].strip()) and RE_PAREN_DESC.match(lines[i + 1].strip()):
            hard.append(f"T4裸标题+括号: {lines[i].strip()[:20]}")
            break

    # T5/T6 孤儿图注/表注：注行之后到下一个标题/标签之间无对应标签
    tag_lis = [(m.start(), m.end()) for m in re.finditer(r"<(?:drawing|chart|table)\b[^>]*/?>", text)]
    for m in RE_TCAP.finditer(text):
        seg_end = _next_boundary(text, m.end())
        if not any(s < seg_end and s >= m.start() - 200 for s, e in tag_lis if text[e - 1:e] != "/>") and \
           not re.search(r"<table\b", text[m.start():seg_end]):
            warn.append(f"T5孤儿表注: {m.group(0)[:22]}")
            if sum(1 for w in warn if w.startswith("T5")) >= 3:
                break
    for m in RE_FCAP.finditer(text):
        seg_end = _next_boundary(text, m.end())
        if not re.search(r"<(?:drawing|chart)\b", text[m.start():seg_end]):
            warn.append(f"T6孤儿图注: {m.group(0)[:22]}")
            if sum(1 for w in warn if w.startswith("T6")) >= 3:
                break

    # T7 摘要区标签残留
    mab = re.search(r"摘\s*要(.*?)关\s*键\s*词", text, re.S)
    if mab and re.search(r"<(?:table|drawing|chart|graphic)\b", mab.group(1)):
        hard.append("T7摘要区标签残留")

    # T9 无编号指代解算: 指代词→同节找锚点(图注/标签/裸标题),无锚=空承诺
    lines9 = text.splitlines()
    heads = [(m.start(), m.group(0)) for m in RE_HEADING.finditer(text)]
    for m in RE_UNNUM_REF.finditer(text):
        seg = text[max(0, m.start() - 12):m.start()]
        if RE_NUMED_REF.search(text[m.start():m.start() + 10]):
            continue
        # 类别名词复合(设计图/施工图/分解图示等)非本文指代
        if re.search(r"(?:设计|施工|地形|平面|立面|断面|区位|结构|分解|示意|装配|系统)", seg):
            continue
        if RE_NOT_REF.search(seg + text[m.start():m.start() + 2]):
            continue
        if RE_SOFTEN.search(seg):
            continue  # 引他文泛指,降级不计
        # 所在节边界: 指代点往前最近标题 → 往后下一标题
        sec_start = max([p for p, _ in heads if p <= m.start()] + [0])
        nxt = [p for p, _ in heads if p > m.start()]
        sec_end = nxt[0] if nxt else len(text)
        # 两级探测: 近窗(前后各300字)有锚=通过; 仅节级有锚=弱告警; 全无=空承诺硬错误
        near = text[max(0, m.start() - 300):min(len(text), m.start() + 300)]
        wide = text[max(0, sec_start - 300):min(len(text), sec_end + 300)]
        has_near = bool(RE_ANCHOR.search(near) or re.search(r"^\d{1,2}-\d{1,3}\s+\S", near, re.M))
        has_wide = bool(RE_ANCHOR.search(wide) or re.search(r"^\d{1,2}-\d{1,3}\s+\S", wide, re.M))
        if not has_near:
            ctx = text[max(0, m.start() - 20):m.start() + 26].replace("\n", " ")
            if has_wide:
                warn.append(f"T9指代锚点仅节级: …{ctx}… (同节有锚但前后300字无锚)")
            else:
                hard.append(f"T9空承诺指代: …{ctx}… (所在节及邻段无任何图/表锚点)")
            # 不提前 break：同篇可能有多个无编号空承诺，全部报告便于一次修完

    # T8 引用悬空（图注意图集合=显式图注+标签title+标签id；机械类常用 id="5-1" 不写title编号）
    intent_figs = set(RE_REF.findall(text))
    cap_figs = set(re.sub(r"^图\s*", "", m.group(0)).split()[0] for m in RE_FCAP.finditer(text)) | set(
        re.findall(r'<(?:drawing|chart)[^>]*?title="[^"]*?图\s?(\d{1,2}[-–]\d{1,3})', text)) | set(
        re.findall(r'<(?:drawing|chart)[^>]*?\bid="(\d{1,2}[-–]\d{1,3})"', text))
    dangling_f = sorted(intent_figs - cap_figs)
    if dangling_f:
        warn.append(f"T8图引用悬空: {','.join(dangling_f[:5])}")
    intent_ts = set(RE_TREF.findall(text))
    cap_ts = set(re.sub(r"^表\s*", "", m.group(0)).split()[0] for m in RE_TCAP.finditer(text)) | set(
        re.findall(r'<table[^>]*?(?:title|caption)="[^"]*?表\s?(\d{1,2}[-–]\d{1,3})', text)) | set(
        re.findall(r'<table[^>]*?\bid="(\d{1,2}[-–]\d{1,3})"', text))
    dangling_t = sorted(intent_ts - cap_ts)
    if dangling_t:
        warn.append(f"T8表引用悬空: {','.join(dangling_t[:5])}")

    # T10 自报清单对账: [FIGURES]块(剥除前留存) vs 实际标签(自报≠实际=立即定位)
    if mrep:
        claimed = re.findall(r"^(图|表)\s?(\d{1,2}[-–]\d{1,3})", mrep.group(1), re.M)
        actual_tag_figs = set(re.findall(r'<(?:drawing|chart)[^>]*?title="[^"]*?图\s?(\d{1,2}[-–]\d{1,3})', text))
        actual_tag_tabs = set(re.findall(r'<table[^>]*?(?:title|caption)="[^"]*?表\s?(\d{1,2}[-–]\d{1,3})', text))
        actual_cap_figs = set(re.sub(r"^图\s*", "", m.group(0)).split()[0] for m in RE_FCAP.finditer(text))
        actual_cap_tabs = set(re.sub(r"^表\s*", "", m.group(0)).split()[0] for m in RE_TCAP.finditer(text))
        # 标签id="5-1"也是实际存在的证据(机械类常用)
        actual_id_figs = set(re.findall(r'<(?:drawing|chart)[^>]*?\bid="(\d{1,2}[-–]\d{1,3})"', text))
        actual_id_tabs = set(re.findall(r'<table[^>]*?\bid="(\d{1,2}[-–]\d{1,3})"', text))
        actual_figs = actual_tag_figs | actual_cap_figs | actual_id_figs
        actual_tabs = actual_tag_tabs | actual_cap_tabs | actual_id_tabs
        claimed_set = {(kind, num) for kind, num in claimed}
        for kind, num in claimed:
            actual = actual_figs if kind == "图" else actual_tabs
            if num not in actual:
                warn.append(f"T10自报≠实际: {kind}{num} 自报有但全文无对应标签/注")
        # 双向对账：实际标签/注也必须出现在清单中，及时发现漏报。
        actual_pairs = {("图", n) for n in actual_figs} | {("表", n) for n in actual_tabs}
        for kind, num in sorted(actual_pairs - claimed_set):
            warn.append(f"T10实际≠自报: {kind}{num} 有标签/注但未列入自报清单")

    return {"hard": hard, "warn": warn}


def _next_boundary(text, pos):
    """从 pos 起找下一个标题行或 3000 字，取近者。"""
    m = RE_HEADING.search(text, pos)
    return m.start() if m else min(pos + 3000, len(text))


def audit_file(path):
    try:
        text = open(path, encoding="utf-8").read()
        r = audit_txt(text)
        return (path, r)
    except Exception as e:
        return (path, {"hard": [f"读取失败:{e}"], "warn": []})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--dir")
    ap.add_argument("--csv", dest="csv_out")
    args = ap.parse_args()
    files = list(args.paths)
    if args.dir:
        files += sorted(glob.glob(os.path.join(args.dir, "*.txt")))
    if not files:
        ap.print_help(); return
    rows, n_hard, n_warn = [], 0, 0
    for f in files:
        p, r = audit_file(f)
        rows.append((os.path.basename(p), "; ".join(r["hard"]) or "—", "; ".join(r["warn"]) or "—"))
        n_hard += len(r["hard"]); n_warn += len(r["warn"])
        flag = "❌" if r["hard"] else ("⚠️" if r["warn"] else "✅")
        print(f"{flag} {os.path.basename(p)[:32]}  hard:{len(r['hard'])} warn:{len(r['warn'])}")
        for h in r["hard"]: print(f"     [hard] {h}")
        for w in r["warn"][:4]: print(f"     [warn] {w}")
    print(f"\n共 {len(files)} 篇: hard {n_hard} 项, warn {n_warn} 项")
    if args.csv_out:
        with open(args.csv_out, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh); w.writerow(["file", "hard(阻断)", "warn(告警)"]); w.writerows(rows)
        print("→", args.csv_out)


if __name__ == "__main__":
    main()
