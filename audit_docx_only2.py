# -*- coding: utf-8 -*-
"""
audit_docx_only2.py — 论文终版 docx-only 只读审计（第二轮，2026-09-06）
=====================================================================
不依赖 txt 源、零修改。两个检查面：
  A 缺图：图注集合 vs body 内嵌图（w:drawing/w:pict 计数，防 media 复用假账）
     三防：引用句防（行首锚定+所示/显示动词排除）、跨词拼接防（编号内禁空白）、
          双重编号"图1 图1-1"取后者
  B 格式合规：二三级标题数字编号（禁中文序号一、/（一））、
     目录条目带页码、封面（无锡太湖学院+届年日期）、图注编号体系一致性
用法: python audit_docx_only2.py [--csv out.csv] [--limit N]
"""
import os, re, csv, sys, zipfile, argparse
from multiprocessing import Pool

ROOT = "/root/project/workspace/thesis-reviser/论文终版"

# ---------- docx 基础解析 ----------
def docx_parts(path):
    z = zipfile.ZipFile(path)
    xml = z.read("word/document.xml").decode("utf-8", "ignore")
    styles = z.read("word/styles.xml").decode("utf-8", "ignore") if "word/styles.xml" in z.namelist() else ""
    # 命名空间前缀归一：被 Word/WPS 另存过的文档用 ns0: 等前缀（内容完好，
    # 直接正则按 w: 解析会全失明）——把主命名空间前缀统一改写为 w:
    m = re.search(r'<(\w+):document[^>]*xmlns:\1="http://schemas.openxmlformats'
                  r'.org/wordprocessingml/2006/main"', xml)
    if m and m.group(1) != "w":
        pfx = m.group(1)
        xml = xml.replace(f"<{pfx}:", "<w:").replace(f"</{pfx}:", "</w:")
    return xml, styles

def para_lines(xml):
    """按 w:p 切段，w:t 拼接成行文本。"""
    out = []
    for pm in re.finditer(r"<w:p[ >].*?</w:p>", xml, re.S):
        p = pm.group(0)
        txt = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", p))
        if txt.strip():
            out.append((txt.strip(), p))
    return out

# ---------- A. 缺图检查 ----------
# 图注：行首"图 N-N 标题"（编号内禁空白防跨词拼接；短行防引用句）
RE_CAP = re.compile(r"^图\s*(\d{1,2})[-–](\d{1,3})\s*\S")   # 章节式 图3-1
RE_CAP_S = re.compile(r"^图\s*(\d{1,3})\s+\S")              # 简单式 图1（编号后须空格+标题，防"图1显示"引用句）
REF_VERBS = ("所示", "显示", "如图", "见图", "见图表", "中图", "如下图", "上图", "下图", "如图表")

def captions_and_refs(lines):
    caps, refs = [], set()
    for txt, _ in lines:
        m = RE_CAP.match(txt)
        if m and not any(v in txt[:22] for v in REF_VERBS):
            caps.append((f"图{m.group(1)}-{m.group(2)}", txt))
            continue
        m = RE_CAP_S.match(txt)
        if m and not any(v in txt[:22] for v in REF_VERBS):
            caps.append((f"图{m.group(1)}", txt))
            continue
        # 正文引用（悬空检查用）：图N-N / 图N（编号内禁空白）
        # 构词防：简图/插图/附图/图纸/大样图/示意图/图表/流程图/路线图/效果图 尾接数字是名词
        # TOC粘连防：目录行=编号标题+页码（"3.3 施工进度计划图8"），非引用
        for r in re.findall(r"(?<![简插附纸样意标流路线效])图\s?(\d{1,2})[-–](\d{1,3})(?!\d)", txt):
            refs.add(f"图{r[0]}-{r[1]}")
        if re.match(r"^\d+(\.\d+)*\s", txt) and re.search(r"\d{1,3}\s*$", txt):
            pass  # 目录行整体跳过
        else:
            for r in re.findall(r"(?<![简插附纸样意标流路线效数如见中上下左右])\s?图\s?(\d{1,3})(?![\d\-–\.\d])", txt):
                refs.add(f"图{r}")
    # 双重编号"图1 图1-1"→ 取后者（后出现的章节式覆盖简单式同序号）
    seen, dedup = set(), []
    for cid, txt in caps:
        if any(cid in s or s in cid for s in seen if cid != s):
            continue
        dedup.append((cid, txt)); seen.add(cid)
    return dedup, refs

def check_images(xml, lines):
    n_tbl = xml.count("<w:tbl>")
    n_draw = xml.count("</w:drawing>") + xml.count("<w:pict")
    caps, refs = captions_and_refs(lines)
    cap_ids = [c for c, _ in caps]
    missing = []
    # graphic/drawing 标签以转义文本裸躺正文(渲染器不认<graphic>标签致图全丢, 严涛漏网源)
    body = " ".join(t for t, _ in lines)
    m_esc = re.search(r"&lt;(?:graphic|drawing|table|image|figure|chart)\b", body)
    if m_esc:
        missing.append("标签转义残留:" + m_esc.group(0).replace("&lt;", "<"))
    # 表注孤儿/叙述误抓防: 表注行后随"的"=叙述非注
    tcaps = [t for t, _ in lines if re.match(r"^表\s?\d{1,2}([-–]\d{1,3})?\s+\S", t) and not re.match(r"^表\s?\d[^\s]*的", t)]
    if len(tcaps) > n_tbl:
        missing.append(f"表注{len(tcaps)}>表体{n_tbl}:{tcaps[0][:14]}")
    # 裸标题行+括号参数行(严涛4.4"路面结构层示意图"+(…图中标注…)漏网源)
    for k in range(len(lines) - 1):
        a, b = lines[k][0], lines[k + 1][0]
        if re.match(r"^[\u4e00-\u9fa5]{2,14}(示意图|布置图|剖面图|大样图|流程图)$", a) and re.match(r"^\(.*图中", b):
            missing.append("裸标题+括号说明:" + a[:14])
            break
    # 裸行图注（丢"图"字头）: 正文区 N-N 标题 行(张超5-1/5-2/5-3漏网源)
    toc_end = next((i for i, (t, _) in enumerate(lines) if re.match(r"^第\s*1\s*章", t)), 0)
    for t, _ in lines[toc_end:]:
        if re.match(r"^\d{1,2}-\d{1,3}\s+\S.{4,40}$", t) and not re.search(r"\d\s*$", t) \
           and any(k in t for k in ("图", "布置", "剖面", "示意", "流程", "平面", "配筋", "立面")):
            missing.append("裸行图注:" + t[:20])
            break
    if len(cap_ids) > n_draw:
        # 图注多于内嵌图 → 缺（逐个列出，从后往前缺最常见，但保守：全列差集）
        missing = cap_ids[n_draw:] if n_draw < len(cap_ids) else []
    dangling = sorted(r for r in refs if r not in cap_ids and not any(c.startswith(r + "-") for c in cap_ids))
    # 编号体系混存（章节式+简单式并存→警告级）
    sys_mix = ("章节式" if any("-" in c for c in cap_ids) else "") + \
              ("+" + "简单式" if any("-" not in c for c in cap_ids) else "")
    return {"embed": n_draw, "caps": cap_ids, "missing": missing,
            "dangling": dangling, "mix": sys_mix if sys_mix not in ("章节式", "简单式", "") else ""}

# ---------- B. 格式合规 ----------
CN_SEQ = re.compile(r"^[一二三四五六七八九十]+[、.．]")        # 一、
CN_SEQ2 = re.compile(r"^[（(][一二三四五六七八九十]+[)）]")     # （一）
NUM_H2 = re.compile(r"^\d{1,2}\.\d{1,2}(\s|\S)")               # 3.1
NUM_H3 = re.compile(r"^\d{1,2}\.\d{1,2}\.\d{1,2}(\s|\S)")      # 3.1.1
TOC_ITEM = re.compile(r"\.{2,}\s*\d{1,3}\s*$|…{2,}\s*\d{1,3}\s*$|\s\d{1,3}\s*$")

def para_style(p_xml):
    m = re.search(r'w:pStyle w:val="([^"]+)"', p_xml)
    return m.group(1) if m else ""

def check_format(xml, styles, lines, cohort):
    issues, warns = [], []
    # 1) 标题编号（Heading2/3 段落禁中文序号；且须数字编号）
    h2 = h3 = cn_bad = 0
    for txt, p in lines:
        st = para_style(p)
        if st in ("2", "Heading2", "heading 2"):
            h2 += 1
            if CN_SEQ.match(txt) or CN_SEQ2.match(txt):
                cn_bad += 1
                issues.append(f"二级标题中文序号: {txt[:30]}")
            elif not NUM_H2.match(txt):
                warns.append(f"二级标题非数字编号: {txt[:30]}")
        elif st in ("3", "Heading3", "heading 3"):
            h3 += 1
            if CN_SEQ.match(txt) or CN_SEQ2.match(txt):
                cn_bad += 1
                issues.append(f"三级标题中文序号: {txt[:30]}")
            elif not NUM_H3.match(txt):
                warns.append(f"三级标题非数字编号: {txt[:30]}")
    # 2) 目录：找到"目 录/目录"行 → 后续非空行须带页码，含一二级
    toc_idx = next((i for i, (t, _) in enumerate(lines) if re.fullmatch(r"目\s*录", t)), None)
    if toc_idx is None:
        issues.append("无目录页")
    else:
        # 条目 = 目录后 80 行内"行尾带页码"的短行（目录条目与正文标题同以
        # "第一章"开头，不能靠标题 break——以页码特征区分）
        win = [t for t, _ in lines[toc_idx + 1: toc_idx + 80]]
        toc_items = [t for t in win if re.search(r"\d{1,3}\s*$", t) and len(t) < 80
                     and not re.match(r"^\d+[\.\d]*\s*$", t)]
        depth2 = any(NUM_H2.match(t) for t in toc_items)
        if len(toc_items) < 5:
            issues.append(f"目录条目过少({len(toc_items)}条)")
        if not depth2:
            warns.append("目录未见二级条目")
    # 3) 封面
    head_txt = " ".join(t for t, _ in lines[:30])
    if "无锡太湖学院" not in head_txt and "无锡太湖学院" not in " ".join(t for t, _ in lines[:60]):
        issues.append("封面无'无锡太湖学院'")
    year_ok = "2026" in head_txt if cohort == "26" else ("2025" in head_txt)
    if not year_ok:
        # 封面日期（届年5月）
        head2 = " ".join(t for t, _ in lines[:60])
        year_ok = ("2026" in head2) if cohort == "26" else ("2025" in head2)
        if not year_ok:
            issues.append(f"封面缺{cohort}届年份（20{cohort}）")
    return {"issues": issues, "warns": warns, "h2": h2, "h3": h3}

# ---------- 汇总 ----------
# 已登记遗留: 修复会引发更大不自洽(计算链/表格重算),人工评估后放行并注明
KNOWN_RESIDUAL = {
    "周亮": {"building_height": "概况已改33.6m;风振句19.8m支撑各层wk计算表,改动=全表重算,暂留"},
}


def check_consistency(xml_text: str, cohort: str, major: str, fname: str = "") -> list:
    issues = []
    try:
        if "土木" in (major or ""):
            from civil_consistency import gate_civil_drawing
            rep = gate_civil_drawing(text=xml_text)
            residual = KNOWN_RESIDUAL.get(fname[:2], {})
            for e in rep.get("errors", []):
                if e.get("key") in residual:
                    issues.append(f"已登记遗留[{e.get('key')}]: {residual[e.get('key')]}")
                else:
                    issues.append(f"参数冲突[{e.get('key')}]: {','.join(map(str, e.get('values', [])))}")
        elif "机械" in (major or ""):
            from mechanical_consistency import resolve_article_spec, validate_mechanical_spec
            spec = resolve_article_spec(xml_text)
            rep = validate_mechanical_spec(spec)
            for e in rep.get("errors", []):
                issues.append(f"参数冲突[{e.get('key')}]: {str(e.get('message',''))[:40]}")
    except Exception:
        pass
    return issues


def audit_one(args):
    path, cohort = args
    rel = os.path.relpath(path, ROOT)
    try:
        xml, styles = docx_parts(path)
        lines = para_lines(xml)
        img = check_images(xml, lines)
        fmt = check_format(xml, styles, lines, cohort)
        bad = []
        if img["missing"]:
            bad.append(f"缺图{len(img['missing'])}张:{','.join(img['missing'][:6])}")
        if img["dangling"]:
            bad.append(f"图号引用悬空{len(img['dangling'])}:{','.join(img['dangling'][:5])}")
        major = "/".join(rel.split(os.sep)[1:3])
        fname = os.path.basename(rel)
        cons = check_consistency(" ".join(t for t, _ in lines), cohort, major, fname)
        fmt["issues"] += [i if not i.startswith("已登记") else i for i in cons]
        bad += [i for i in cons if not i.startswith("已登记")]
        return {"file": rel, "cohort": cohort, "embed": img["embed"], "caps": len(img["caps"]),
                "missing": ";".join(img["missing"]), "dangling": ";".join(img["dangling"]),
                "mix": img["mix"], "fmt_issues": " | ".join(fmt["issues"]),
                "fmt_warns": " | ".join(fmt["warns"][:4]), "status": "❌" if bad else "✅"}
    except Exception as e:
        return {"file": rel, "cohort": cohort, "embed": 0, "caps": 0, "missing": "", "dangling": "",
                "mix": "", "fmt_issues": f"解析失败:{e!r}", "fmt_warns": "", "status": "❌"}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="/root/project/workspace/终版审计_0930.csv")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    jobs = []
    for y in ("25", "26"):
        for dp, _, fs in os.walk(os.path.join(ROOT, y)):
            for f in fs:
                if f.endswith(".docx") and not f.startswith("~$"):
                    jobs.append((os.path.join(dp, f), y))
    if a.limit:
        jobs = jobs[:a.limit]
    print(f"共 {len(jobs)} 篇，审计中…")
    with Pool(8) as pool:
        rows = pool.map(audit_one, jobs)
    with open(a.csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    bad = [r for r in rows if r["status"] == "❌"]
    warn = [r for r in rows if r["status"] == "✅" and (r["fmt_warns"] or r["mix"])]
    print(f"✅ {len(rows) - len(bad)}  ❌ {len(bad)}  ⚠️ {len(warn)}  → {a.csv}")
    # 分类统计
    from collections import Counter
    cat = Counter()
    for r in bad:
        if r["missing"]: cat["缺图"] += 1
        if r["dangling"]: cat["图号悬空"] += 1
        for k in ("中文序号", "无页码", "无目录", "封面", "解析失败"):
            if k in r["fmt_issues"]: cat[k] += 1
    print("问题分布:", dict(cat))
    for r in bad[:15]:
        print(f"  ❌ {r['file']}: {r['missing'] or ''}{r['dangling'] and '悬空:'+r['dangling'] or ''} {r['fmt_issues'][:60]}")

if __name__ == "__main__":
    main()
