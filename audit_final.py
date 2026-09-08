# -*- coding: utf-8 -*-
"""论文终版 docx-only 审计器(无过程文件场景)。
从 docx 本体直读: 封面/摘要/关键词/目录/章节字数/图片(空白检测)/表格(占比分组)/
图X引用与编号/参考文献/占位残留。输出结构化结果供批量驱动。

用法(单篇): python3 audit_final.py --file X.docx --type 管理
用法(批量): python3 audit_final.py --root 论文终版 --csv 报告.csv
"""
import os, re, sys, io, csv, json, zipfile, argparse
from docx import Document

TYPE_DIR = {"土木": "土木", "机械": "机械", "经管": "管理", "设计": "设计", "法学": "法学"}
WORD_MIN = {"管理": 800, "机械": 800, "土木": 800, "设计": 900, "法学": 900}
WORD_MIN_SPECIAL = 500  # 绪论/结论/总结/参考文献章
CH_HEAD = re.compile(r"^(第[一二三四五六七八九十\d]+章)\s*(\S.{0,28})$")
CH_HEAD.match  # noqa
CAPT_RE = {"图": re.compile(r"^(图\s*(\d{1,2}))[  \u3000]*\S"), "表": re.compile(r"^(表\s*(\d{1,2}))[  \u3000]*\S")}
REF_RE = re.compile(r"[如见]?[图表]\s*(\d{1,2})")
PLACEHOLDER_RE = re.compile(r"占位|图缺(?!少|失)|此处插入|请替换|XXX+|\[待补充\]|\[待完善\]|【待补充】")


def _img_blank(b: bytes) -> bool:
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(b)).convert("L")
        if im.size[0] < 60 or im.size[1] < 60:
            return True
        g = im.resize((64, 64))
        px = list(g.getdata())
        mean = sum(px) / len(px)
        var = sum((p - mean) ** 2 for p in px) / len(px)
        return var < 40
    except Exception:
        return False


def load_docx(path):
    d = Document(path)
    paras = [p.text for p in d.paragraphs]
    tables = [[[c.text.strip() for c in row.cells] for row in t.rows] for t in d.tables]
    z = zipfile.ZipFile(path)
    imgs = []
    for n in sorted(z.namelist()):
        if n.startswith("word/media/"):
            imgs.append((n, z.read(n)))
    return paras, tables, imgs


def _pct_check(tbl, warn_prefix):
    """占比列分组检查(复用audit_docx规则: DP分组+行向豁免+变化率列排除)"""
    if not tbl or len(tbl) < 2:
        return None
    headers = [h for h in tbl[0]]
    body = tbl[1:]
    RATE = re.compile(r"提升|增长|同比|变化|降幅|涨幅|增速|增幅|提高|降低|下降|波动")
    for off in (0, 1):
        eff = headers[off:]
        # 仅构成语义表头(占比/构成/比重/份额)才要求和=100; 率类指标列(毛利率/达标率)是每行独立指标
        # 年份列表头(如"2021年股权融资占比")是跨年时间序列, 不做构成检查
        if any(re.search(r"\d{4}年", h) for i, h in enumerate(eff) if h and re.search(r"占比|构成|比重|份额", h)):
            return None
        pct_idx = [i for i, h in enumerate(eff) if h
                   and re.search(r"占比|构成|比重|份额", h) and not RATE.search(h)]
        if not pct_idx:
            continue
        if len(pct_idx) >= 2:  # 行向构成豁免: 仅汇总占比语义列(如 原材料占比+在产品占比+产成品占比=100)
            n_rows = row_ok = 0
            for row in body:
                vals = []
                for c_i in pct_idx:
                    if c_i < len(row):
                        m = re.search(r"([\d.]+)", row[c_i] or "")
                        if m: vals.append(float(m.group(1)))
                if len(vals) >= 2:
                    n_rows += 1
                    if 90 <= sum(vals) <= 110:
                        row_ok += 1
            if n_rows and row_ok >= n_rows * 0.8:
                return None
        # 年份行豁免: 行名列过半为年份(如2019/2020年) → 占比是时间序列而非构成
        namecol = 0 if off == 0 else None
        yearish = 0
        if namecol == 0:
            for row in body:
                nm = (row[0] if row else "") or ""
                yearish += bool(re.match(r"^\d{4}", nm) or re.search(r"[年月季度]", nm) and re.search(r"\d", nm))
            if body and yearish >= len(body) * 0.5:
                return None
        col = pct_idx[0]
        vals = []
        for row in body:
            if col < len(row):
                m = re.search(r"([\d.]+)", row[col] or "")
                if m:
                    vals.append(float(m.group(1)))
        if len(vals) < 3:
            continue
        # DP: 可否切成组内和∈[90,110]的连续段
        dp = {0: True}
        for i in range(1, len(vals) + 1):
            dp[i] = False
            for j in range(i):
                if dp.get(j) and 90 <= sum(vals[j:i]) <= 110:
                    dp[i] = True
                    break
        if not dp.get(len(vals)):
            return f"{warn_prefix}{eff[col]}\"占比列无法按100%分组(合计{sum(vals):g}%)"
        return None
    return None


def audit_docx_only(path, ptype):
    paras, tables, imgs = load_docx(path)
    _doc = Document(path)
    text = "\n".join(paras)
    res = {"file": path, "type": ptype, "problems": [], "verdict": "✅",
           "stats": {}, "fixes": []}

    # ---- 封面(前10段应含关键字段) ----
    head = "\n".join(paras[:10])
    for fld in ("姓", "学", "专", "层", "指导教师"):
        if fld not in head:
            res["problems"].append(f"封面缺字段[{fld}]")

    # ---- 摘要/关键词/目录 ----
    if "摘要" not in text[:4000]:
        res["problems"].append("缺摘要")
    else:
        m = re.search(r"摘要\s*\n(.{80,})", text, re.S)
        if not m:
            res["problems"].append("摘要过短")
    if not re.search(r"关键词", text[:6000]):
        res["problems"].append("缺关键词")
    if "目录" not in text[:8000]:
        res["problems"].append("缺目录")

    # ---- 章节(剥离目录区: 首个正文章头之前带\t页码的行) ----
    body_start = 0
    for i, p in enumerate(paras):
        if re.match(r"^第[一二三四五六七八九十\d]+章", p.strip()) and "\t" not in p:
            body_start = i
            break
    body = paras[body_start:]
    toc_zone = "\n".join(paras[:body_start])
    chs = []  # (title, start_idx)
    for i, p in enumerate(body):
        s = p.strip()
        if re.match(r"^第[一二三四五六七八九十\d]+章", s) and "\t" not in s and len(s) < 80 \
                and not re.search(r"[。；]", s):
            chs.append((s, i))
    res["stats"]["chapters"] = len(chs)
    for k, (title, si) in enumerate(chs):
        ei = chs[k + 1][1] if k + 1 < len(chs) else len(body)
        wc = sum(len(x) for x in body[si:ei])
        t = title[:6]
        if re.search(r"绪论|引言|结论|总结|参考文献|致谢", title):
            need = WORD_MIN_SPECIAL
        else:
            need = WORD_MIN.get(ptype, 800)
        if wc < need * 0.6:
            res["problems"].append(f"{t}字数过短({wc}<{need})")
        if wc > 20000:
            res["problems"].append(f"{t}字数异常({wc})")

    # ---- 图片与空白检测(内嵌drawing计数; media文件可能被复用) ----
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    n_img = len(_doc.element.body.findall(f".//{W}drawing")) + len(_doc.element.body.findall(f".//{W}pict"))
    blanks = [os.path.basename(n) for n, b in imgs if _img_blank(b)]
    res["stats"]["images"] = n_img
    res["stats"]["blank_images"] = len(blanks)
    if blanks:
        res["problems"].append(f"空白图{len(blanks)}张")

    # ---- 图/表 编号(兼容简单式图1..N与章节式图C-N)与引用 ----
    capt_line = re.compile(r"^([图表])")
    _tok = re.compile(r"([图表])\s*(\d{1,2})(?:\s*[-–—]\s*(\d{1,2}))?(?=\s|$)")
    _REF_SENT = re.compile(r"显示|如下|所示|可知|可以看出|中可|给出|列出|反映了|表明")
    for kind in ("图", "表"):
        caps = []          # [(line_idx, C, N_or_None, full)]
        for i, p in enumerate(paras):
            s = p.strip()
            if _REF_SENT.search(s[:20]) and not re.match(r'^图\s*\d', s):
                continue  # 明显引用句排除；带图号的长图注仍必须识别
            found = []
            if capt_line.match(s):
                for m in _tok.finditer(s[:44]):
                    if m.group(1) != kind:
                        continue
                    found.append((int(m.group(2)), int(m.group(3)) if m.group(3) else None))
            if found:
                caps.append((i, found, s[:30]))
        # 同一注行出现多个同类编号即报告，不能只取最后一个掩盖重复/双重编号。
        dbl = [s for _, f, s in caps if len(f) > 1]
        if dbl:
            res["problems"].append(f"{kind}注双重编号{len(dbl)}处(如\"{dbl[0][:18]}\")")
        # 保留每个编号并检测重复；每行第一个/最后一个都不能静默吞掉。
        nums = [f[-1] for _, f, _ in caps]
        from collections import Counter
        dup_nums = [n for n, count in Counter(nums).items() if count > 1]
        if dup_nums:
            res["problems"].append(f"{kind}注编号重复{len(dup_nums)}个({dup_nums[:5]})")
        simple = [c for c, n in nums if n is None]
        chapter = [(c, n) for c, n in nums if n is not None]
        # 引用集合(全文)
        ref_re = re.compile(rf"[如见]?([图表])\s*(\d{{1,2}})(?:\s*[-–—]\s*(\d{{1,2}}))?")
        refs = set()
        # 引用扫描范围=正文段落+表格单元格("详见表2-1"可能写在表内)
        _corpus = list(body) + [c for tbl in tables for row in tbl for c in row if c]
        for p in _corpus:
            for m in ref_re.finditer(p):
                if m.group(1) != kind:
                    continue
                refs.add((int(m.group(2)), int(m.group(3)) if m.group(3) else None))
        cap_set = set(nums)
        bad_refs = sorted(r for r in refs if r not in cap_set)
        # 裸"图K"引用在章节式论文中若存在章节K也视为合法
        if chapter:
            ch_first = {c for c, _ in chapter}
            bad_refs = [r for r in bad_refs if not (r[1] is None and r[0] in ch_first)]
        if bad_refs:
            fmt = ",".join(f"{kind}{c}-{n}" if n else f"{kind}{c}" for c, n in bad_refs[:4])
            res["problems"].append(f"{kind}引用失配{len(bad_refs)}处(如{fmt})")
        # 简单式连续性(仅当无章节式)
        if not chapter and simple:
            if sorted(simple) != list(range(1, len(simple) + 1)):
                res["problems"].append(f"{kind}注编号不连续{sorted(simple)[:8]}")
        # 章节式: 各章内N连续
        if chapter:
            by_ch = {}
            for c, n in chapter:
                by_ch.setdefault(c, []).append(n)
            gaps = [f"{kind}{c}-{ns}" for c, ns in by_ch.items()
                    if sorted(ns) != list(range(1, len(ns) + 1))]
            if gaps:
                res["problems"].append(f"{kind}注章节编号不连续({';'.join(gaps[:3])})")
        # 图注数与图片数匹配
        if kind == "图":
            n_figcaps = len(caps)
            if n_figcaps and abs(n_figcaps - n_img) > 0:
                res["problems"].append(f"图注{n_figcaps}个vs图片{n_img}张不匹配")
    res["stats"]["tables"] = len(tables)

    # ---- 表格占比 ----
    for ti, tbl in enumerate(tables):
        t = tbl[0][0] if tbl and tbl[0] and tbl[0][0] else f"表{ti+1}"
        w = _pct_check(tbl, f"表\"")
        if w:
            res["problems"].append(w)

    # ---- 参考文献 ----
    m = re.search(r"参考文献\s*\n((?:\[?\d+\]?[^\n]+\n?)+)", text)
    if not m:
        m = re.search(r"参考文献\s*\n(.{200,})", text, re.S)
    ref_line_re = re.compile(r'^\s*(?:\[\s*(\d+)\s*\]|［\s*(\d+)\s*］|(\d+)\.)', re.M)
    refs = []
    if m:
        for rm in ref_line_re.finditer(m.group(1)):
            refs.append(next((g for g in rm.groups() if g), ""))
    n_refs = len(set(refs))
    res["stats"]["refs"] = n_refs
    if n_refs < 10:
        res["problems"].append(f"参考文献不足({n_refs}<10)")
    if m and not refs:
        res["problems"].append("参考文献无[1][2]编号条目")

    # ---- 占位残留 ----
    ph = PLACEHOLDER_RE.findall(text)
    if ph:
        res["problems"].append(f"占位残留{len(ph)}处")

    # ---- 法学专项(评阅必达项) ----
    if ptype == "法学":
        res["stats"].update(_law_checks(paras, text, res))

    # ---- 判级: 结构性缺陷阻断, 版式类警告 ----
    hard = re.search(r"空白图|缺摘要|缺关键词|缺目录|参考文献不足|占位残留|封面缺字段|"
                     r"引用失配|编号不连续|双重编号|无法解析|"
                     r"法学正文不足|法学摘要字数|法学关键词数|缺案例来源|缺案号", ";".join(res["problems"]))
    if hard:
        res["verdict"] = "❌"
    elif res["problems"]:
        res["verdict"] = "⚠️"
    return res


def _law_checks(paras, text, res):
    """法学本科论文评阅必达项: 正文≥8000 / 摘要300-500 / 关键词3-5 / 文献≥10 / 案例真实+标注来源"""
    st = {}
    body_start = 0
    for i, p in enumerate(paras):
        s = p.strip()
        if re.match(r"^第[一二三四五六七八九十\d]+章", s) and "\t" not in s:
            body_start = i
            break
    head = paras[body_start] if body_start < len(paras) else ""
    m_ref = re.search(r"^参考文献$", text, re.M)
    core = text[text.find(head):m_ref.start()] if (m_ref and head) else text
    n = len(re.sub(r"\s|第[一二三四五六七八九十\d]+章[^\n]{0,30}|\d+\.\d+(\.\d+)?[^\n]{0,30}", "", core))
    st["law_body_words"] = n
    if n < 8000:
        res["problems"].append(f"法学正文不足({n}<8000)")
    m = re.search(r"摘要\s*\n(.+?)\n", text, re.S)
    if m:
        na = len(re.sub(r"\s", "", m.group(1)))
        st["law_abstract"] = na
        if not (300 <= na <= 500):
            res["problems"].append(f"法学摘要字数({na}不在300-500)")
    m = re.search(r"关键词\s*\n(.+)", text)
    if m:
        nk = len([k for k in re.split(r"[;；]", m.group(1)) if k.strip()])
        st["law_keywords"] = nk
        if not (3 <= nk <= 5):
            res["problems"].append(f"法学关键词数({nk}不在3-5)")
    if not re.search(r"指导性案例|指导案例\d+号|裁判文书网|北大法宝|公报|典型案例", text):
        res["problems"].append("缺案例来源标注")
    if not re.search(r"指导案例\s*\d+\s*号", text):
        res["problems"].append("缺案号(指导案例N号)")
    return st


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", help="论文终版根目录(含25/26)")
    ap.add_argument("--file", help="单篇审计")
    ap.add_argument("--type", default="管理")
    ap.add_argument("--csv", help="输出CSV")
    ap.add_argument("--skip", help="跳过学号清单文件(JSON数组, 归一化无前导0)")
    args = ap.parse_args()

    results = []
    if args.file:
        results.append(audit_docx_only(args.file, args.type))
    else:
        skip = set(json.load(open(args.skip))) if args.skip else set()
        for year in ("25", "26"):
            for tdir, ptype in TYPE_DIR.items():
                d = os.path.join(args.root, year, tdir)
                if not os.path.isdir(d):
                    continue
                for fn in sorted(os.listdir(d)):
                    if not fn.endswith(".docx"):
                        continue
                    m = re.search(r"_0*(\d+)\.docx$", fn)
                    sid = (m.group(1).lstrip("0") or m.group(1)) if m else None
                    if sid in skip:
                        results.append({"file": os.path.join(d, fn), "type": ptype,
                                        "problems": ["SKIP(已用新生成版替换)"], "verdict": "⏭️",
                                        "stats": {}, "fixes": []})
                        continue
                    try:
                        results.append(audit_docx_only(os.path.join(d, fn), ptype))
                    except Exception as e:
                        results.append({"file": os.path.join(d, fn), "type": ptype,
                                        "problems": [f"无法解析: {e}"], "verdict": "❌",
                                        "stats": {}, "fixes": []})
    # 汇总
    from collections import Counter
    c = Counter(r["verdict"] for r in results)
    print(f"审计 {len(results)} 篇: " + " ".join(f"{k}{v}" for k, v in c.most_common()))
    for r in results:
        if r["verdict"] in ("❌", "⚠️"):
            print(f"  {r['verdict']} {os.path.relpath(r['file'], args.root or '.')[:46]} {('; '.join(r['problems']))[:90]}")
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["文件", "类型", "终审", "章数", "图", "空白图", "表", "文献", "问题"])
            for r in results:
                s = r["stats"]
                w.writerow([os.path.relpath(r["file"], args.root or "."), r["type"], r["verdict"],
                            s.get("chapters", ""), s.get("images", ""), s.get("blank_images", ""),
                            s.get("tables", ""), s.get("refs", ""), "; ".join(r["problems"])])
        print(f"CSV: {args.csv}")


if __name__ == "__main__":
    main()
