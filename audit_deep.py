# -*- coding: utf-8 -*-
"""深度审计器 v2 (2026-09-07) — 无源论文全面复审。

在 audit_final.py 全部检查项(封面/摘要/关键词/章节字数/空白图/图表编号引用/
占比构成/文献数/占位残留)基础上新增 8 类维度:

  D1 目录完整性      — 目录行页码存在; 目录标题 vs 正文标题双向对照(失配/缺页码)
  D2 标题编号规范    — 二级标题 N.1 形式; 禁中文序号标题; 章"第N章"连续不跳号;
                        禁超3级(1.1.1.1)
  D3 表注vs表格数    — "表N"注行数 vs docx.tables 数失配(同图注逻辑)
  D4 中英摘要        — 中文摘要/关键词必须; Abstract/Key words 收集性检查(⚠️)
  D5 docx健康        — 修订痕迹(w:ins/w:del)/批注/TOC域未更新/正文裸URL/空段落占比
  D6 篇内重复段      — 归一化3-gram重叠>60%且>150字的段对(LLM复读/章节互抄)
  D7 引用-文献对账   — 正文[N]引用编号 ⊆ 参考文献列表编号; 未引用文献数
  D8 全篇字数        — 正文区中文字符下限(经管10000/工科8000, ⚠️级)

用法: python3 audit_deep.py --list 论文修订档案/_无源清单_0908.json \
          --csv 论文修订档案/审计报告_0908深度.csv [--root 论文终版]
"""
import os, re, sys, csv, json, zipfile, argparse, glob
from collections import Counter, defaultdict
from docx import Document

# ---- 复用 audit_final 的基础检查(封面/图表编号/占比/占位/空白图) ----
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audit_final as AF

CH_HEAD = AF.CH_HEAD
PLACEHOLDER_RE = AF.PLACEHOLDER_RE

# ---- 领域常量 ----
BODY_CHARS_MIN = {"管理": 10000, "经管": 10000, "机械": 8000, "土木": 8000}
SUB_HEAD = re.compile(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{1,2}))?(?:\.(\d{1,2}))?\s*\S")
CN_HEAD = re.compile(r"^([一二三四五六七八九十]{1,3})[、．.]\s*(\S.{0,30})$")
CN_HEAD2 = re.compile(r"^[（(]([一二三四五六七八九十]{1,3})[）)]\s*(\S.{0,30})$")
TOC_TITLE = re.compile(r"^\s*目\s*[\u3000 ]*\s*录\s*$")
ABSTRACT_CN = re.compile(r"^\s*摘\s*[\u3000 ]*\s*要\s*$")
ABSTRACT_EN = re.compile(r"^\s*Abstract\s*:?\s*$', |^\s*A\s?B\s?S\s?T\s?R\s?A\s?C\s?T\b", re.I)
KEYWORDS_EN = re.compile(r"Key\s*words?\s*[:：]", re.I)
REF_CITE = re.compile(r"\[(\d{1,3})(?:[,，\-–]\d{1,3})*\]")
URL_RE = re.compile(r"https?://\S{18,}")
BLANK_OK = re.compile(r"^[\s\u3000.·…—\-]*$")


def zh_len(s: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", s or ""))


# ---------------------------------------------------------------- 段落分区
def _toc_line(ts: str) -> bool:
    """目录条目特征: 标题形态 + 行尾页码( '...概况\\t1' / '...设计……3' )。"""
    if not re.search(r"[\t\u3000 .…·]\d{1,3}$", ts):
        return False
    return bool(CH_HEAD.match(ts) or SUB_HEAD.match(ts) or re.match(r"^(参考文献|致谢|结论|附录)", ts))


def split_zones(paras):
    """把段落切成 (前言区, 目录区, 正文区, 文献区, 尾部区)。
    目录区= 目录标题→第一个**不带页码尾巴**的章标题(目录条目自身也是章标题形态,
    靠行尾页码区分); 文献区= 参考文献标题→致谢/附录/结尾。"""
    n = len(paras)
    toc_start = body_start = ref_start = tail_start = None
    for i, t in enumerate(paras):
        ts = t.strip()
        if toc_start is None and i < n * 0.4 and TOC_TITLE.match(ts):
            toc_start = i
        elif toc_start is not None and body_start is None and CH_HEAD.match(ts) and i > toc_start:
            if not _toc_line(ts):  # 带页码尾巴的是目录条目, 不是正文标题
                body_start = i
        if body_start is not None and ref_start is None and re.match(r"^\s*参考文献\s*$|^\s*参\s*考\s*文\s*献\s*$", ts):
            ref_start = i
        if ref_start is not None and tail_start is None and re.match(r"^\s*(致\s*谢|谢\s*辞|附\s*录|攻读)", ts):
            tail_start = i
    if toc_start is None:
        toc_start = body_start = 0
    if body_start is None:
        body_start = toc_start
    if ref_start is None:
        ref_start = n
    if tail_start is None:
        tail_start = n
    return {
        "pre": paras[:toc_start],
        "toc": paras[toc_start:body_start],
        "body": paras[body_start:ref_start],
        "refs": paras[ref_start:tail_start],
        "tail": paras[tail_start:],
        "toc_range": (toc_start, body_start), "ref_range": (ref_start, tail_start),
    }


# ---------------------------------------------------------------- D1 目录
def audit_toc(zones):
    problems, notes = [], {}
    toc_lines = [t.strip() for t in zones["toc"] if t.strip() and not TOC_TITLE.match(t.strip())]
    if not toc_lines:
        return ["❌未找到目录"], {"toc_lines": 0}
    entries = []  # (标题, 页码 or None)
    for ln in toc_lines:
        m = re.match(r"^(.*?)[\t\s\u3000.…·]+(\d{1,3})$", ln)
        if m:
            entries.append((re.sub(r"[\s\u3000]+$", "", m.group(1)), int(m.group(2))))
        else:
            entries.append((ln, None))
    no_page = [e for e in entries if e[1] is None]
    notes["toc_lines"] = len(entries)
    if no_page:
        problems.append(f"⚠️目录{len(no_page)}行无页码")
    # 正文标题集合(排除行尾带页码的目录条目形态)
    body_heads = []
    for t in zones["body"]:
        ts = t.strip()
        if _toc_line(ts):
            continue
        if CH_HEAD.match(ts) or SUB_HEAD.match(ts):
            body_heads.append(ts)
    def norm(s):
        return re.sub(r"[\s\u3000]+", "", s)
    body_set = {norm(h) for h in body_heads}
    toc_set = {norm(e[0]) for e in entries}
    only_toc = [e for e in entries if norm(e[0]) not in body_set
                and not re.match(r"^(参考文献|致谢|结论|附录)", e[0])]
    if only_toc:
        problems.append(f"❌目录{len(only_toc)}条正文无对应:" + ";".join(o[0][:18] for o in only_toc[:3]))
    miss_body = [h for h in body_heads if norm(h) not in toc_set and CH_HEAD.match(h)]
    if miss_body:
        problems.append(f"⚠️{len(miss_body)}个章标题未入目录:" + ";".join(h[:16] for h in miss_body[:3]))
    return problems, notes


# ---------------------------------------------------------------- D2 标题规范
def audit_heads(zones):
    problems = []
    chapters, seen_deep, cn_titles = [], [], []
    for t in zones["body"]:
        ts = t.strip()
        if _toc_line(ts):
            continue  # 目录条目混入防御
        m = CH_HEAD.match(ts)
        if m:
            cn = re.sub(r"^第([一二三四五六七八九十\d]+)章.*", lambda x: x.group(1), ts)
            chapters.append(cn)
            continue
        m2 = SUB_HEAD.match(ts)
        if m2:
            if m2.group(4):
                seen_deep.append(ts[:24])
            continue
        if len(ts) < 38 and (CN_HEAD.match(ts) or CN_HEAD2.match(ts)) and zh_len(ts) > 3:
            # 疑似中文序号标题(短独立行) — 保守⚠️
            cn_titles.append(ts[:24])
    def cn2num(s):
        tbl = {"一":1,"二":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,"十":10}
        if s.isdigit(): return int(s)
        if s == "十": return 10
        if len(s) == 2 and s[0] in tbl and s[1] == "十": return tbl[s[0]] * 10
        if s.startswith("十") and len(s) == 2: return 10 + tbl.get(s[1], 0)
        return None
    nums = [cn2num(c) for c in chapters]
    nums = [x for x in nums if x]
    if nums:
        expect = list(range(nums[0], nums[0] + len(nums)))
        if nums != expect:
            problems.append(f"⚠️章序号不连续:{chapters[:8]}")
    if seen_deep:
        problems.append(f"❌{len(seen_deep)}个超3级标题(1.1.1.1):" + ";".join(seen_deep[:2]))
    if cn_titles:
        problems.append(f"⚠️疑似中文序号标题{len(cn_titles)}处:" + ";".join(cn_titles[:2]))
    return problems, {"chapters": chapters}


# ---------------------------------------------------------------- D3-D7
def audit_tables_notes(zones, tables):
    cap_n = 0
    for t in zones["body"]:
        if re.match(r"^表\s*\d{1,2}(\.\d{1,2})?", t.strip()):
            cap_n += 1
    real = len(tables)
    probs = []
    if cap_n == 0 and real > 0:
        probs.append(f"❌{real}个表格全无表注")
    elif cap_n > 0 and real > 0 and abs(cap_n - real) > max(1, int(real * 0.15)):
        probs.append(f"⚠️表注{cap_n}vs表格{real}失配")
    return probs, {"table_caps": cap_n, "tables": real}


def audit_abstracts(zones, paras):
    text_all = "\n".join(paras)
    probs = []
    if not ABSTRACT_CN.match("\n".join(zones["pre"])) and "摘要" not in "\n".join(zones["pre"])[:600]:
        probs.append("❌前置区无中文摘要标题")
    if not re.search(r"关键词\s*[:：]", "\n".join(zones["pre"]) + "\n".join(zones["toc"])[:200]):
        if not re.search(r"关键词\s*[:：]", text_all[:3000]):
            probs.append("❌未找到中文关键词")
    has_en = bool(re.search(r"Abstract", text_all[:20000], re.I))
    has_en_kw = bool(KEYWORDS_EN.search(text_all[:20000]))
    notes = {"abstract_en": has_en, "keywords_en": has_en_kw}
    # 抽样10篇9篇无Abstract: 本批共性(自考论文不强制英文摘要), 仅记notes不计问题
    if has_en and not has_en_kw:
        probs.append("⚠️有Abstract无Key words")
    return probs, notes


def audit_docx_health(path, zones, paras):
    probs = []
    z = zipfile.ZipFile(path)
    names = z.namelist()
    doc_xml = z.read("word/document.xml").decode("utf-8", "replace")
    if "<w:ins " in doc_xml or "<w:del " in doc_xml:
        probs.append("❌存在修订痕迹(w:ins/w:del)未接受")
    if any("comments" in n for n in names):
        probs.append("⚠️存在批注(comments)")
    if re.search(r"TOC \\o|TOC \\\\o", doc_xml):
        probs.append("⚠️目录为TOC域(可能未刷新页码)")
    urls = [u for t in zones["body"] for u in URL_RE.findall(t)]
    if urls:
        probs.append(f"⚠️正文裸URL {len(urls)}处")
    empty = sum(1 for t in paras if not t.strip())
    if paras and empty / len(paras) > 0.35:
        probs.append(f"⚠️空段落占比{empty}/{len(paras)}")
    return probs, {"urls": len(urls)}


def audit_duplicates(zones):
    """篇内重复段: 归一化后 3-gram 重叠率>60% 且长度>150字。"""
    segs = []
    buf = []
    for t in zones["body"]:
        ts = t.strip()
        if len(ts) < 40:
            if sum(zh_len(x) for x in buf) > 150:
                segs.append("".join(buf))
            buf = []
        else:
            buf.append(ts)
    if sum(zh_len(x) for x in buf) > 150:
        segs.append("".join(buf))
    segs = [s for s in segs if zh_len(s) > 150]
    def grams(s, k=60):
        s = re.sub(r"[\s\u3000，。；：、]", "", s)
        return {s[i:i+k] for i in range(0, max(1, len(s) - k), 30)}
    dup_pairs = []
    gs = [grams(s) for s in segs]
    for i in range(len(segs)):
        for j in range(i + 1, len(segs)):
            if not gs[i] or not gs[j]:
                continue
            ov = len(gs[i] & gs[j]) / min(len(gs[i]), len(gs[j]))
            if ov > 0.6:
                dup_pairs.append((i, j, round(ov, 2)))
    probs = []
    if dup_pairs:
        probs.append(f"❌篇内重复段{len(dup_pairs)}对(重叠度{max(d[2] for d in dup_pairs)})")
    return probs, {"dup_pairs": dup_pairs[:5], "segments": len(segs)}


def audit_citations(zones, path=None):
    body_text = "\n".join(zones["body"])
    cites = set()
    for m in REF_CITE.finditer(body_text):
        for x in re.findall(r"\d{1,3}", m.group(0)):
            cites.add(int(x))
    refs = [t.strip() for t in zones["refs"] if re.match(r"^\[\d+\]", t.strip())]
    ref_nums = set()
    for r in refs:
        m = re.match(r"^\[(\d+)\]", r)
        if m:
            ref_nums.add(int(m.group(1)))
    probs = []
    if not refs:
        return ["❌参考文献区无[编号]条目"], {"refs": 0, "cites": len(cites)}
    over = sorted(c for c in cites if c > max(ref_nums))
    if over:
        probs.append(f"❌正文引用超出文献范围:{over[:6]}")
    # 作者-年份式引用(张三(2020)/李四等（2019）)也是有效标注 — 全文XML检测更稳
    ay = 0
    if path:
        try:
            z = zipfile.ZipFile(path)
            doc = z.read("word/document.xml").decode("utf-8", "replace")
            txt = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", doc))
            ay = len(re.findall(r"[\u4e00-\u9fff]{2,4}(?:等)?[（(]20\d{2}[）)]", txt))
        except Exception:
            pass
    uncited = len(ref_nums) - len(cites & ref_nums)
    if uncited > len(ref_nums) * 0.5:
        if ay >= 3:
            pass  # 作者-年份式引用体系, 非零引用 — 记notes不报问题
        else:
            probs.append(f"⚠️{uncited}/{len(ref_nums)}条文献未被引用")
    return probs, {"refs": len(ref_nums), "cites": len(cites), "uncited": uncited, "author_year": ay}


def audit_body_chars(zones, ptype, tables):
    body = "\n".join(zones["body"])
    tbl_txt = "\n".join(c for t in tables for row in t for c in row)
    n = zh_len(body) + zh_len(tbl_txt)  # 学术字数惯例: 表格内容计入
    mn = BODY_CHARS_MIN.get(ptype, 8000)
    probs = []
    if n < mn * 0.75:
        probs.append(f"❌正文仅{ n }字<{mn*0.75:.0f}")
    elif n < mn:
        probs.append(f"⚠️正文{n}字略少于{mn}")
    return probs, {"body_chars": n}


# ---------------------------------------------------------------- 主审计
def audit_one(path, ptype):
    problems, notes = [], {}
    try:
        paras, tables, imgs = AF.load_docx(path)
    except Exception as e:
        return {"file": path, "verdict": "❌", "problems": [f"无法解析:{e}"], "notes": {}}
    zones = split_zones(paras)
    # 若同目录存在旁车TXT，终审必须回溯TXT层；无TXT时明确记录 unavailable，不能冒充全链路通过。
    txt_candidates = [path[:-5] + ".txt", os.path.splitext(path)[0] + ".txt"]
    txt_path = next((p for p in txt_candidates if os.path.isfile(p)), None)
    if txt_path:
        try:
            from audit_txt import audit_file as _audit_txt_file
            _, tx = _audit_txt_file(txt_path)
            notes["audit_txt"] = {"path": txt_path, "status": "ok" if not tx["hard"] else "failed", "hard": tx["hard"], "warn": tx["warn"]}
            problems += [f"TXT:{x}" for x in tx["hard"]]
        except Exception as e:
            problems.append(f"❌TXT审计异常:{type(e).__name__}:{str(e)[:80]}")
    else:
        notes["audit_txt"] = {"path": "", "status": "unavailable"}
        problems.append("⚠️TXT审计不可用：未找到同名TXT，不能证明TXT层通过")
    # 深审必须显式包含基础终审，不能仅凭注释声称“继承”。
    try:
        base = AF.audit_docx_only(path, ptype)
        problems += list(base.get("problems", []))
        notes["audit_final"] = {"verdict": base.get("verdict"), "stats": base.get("stats", {})}
    except Exception as e:
        problems.append(f"❌基础终审异常:{type(e).__name__}:{str(e)[:80]}")
    steps = [
        ("D1目录", lambda: audit_toc(zones)),
        ("D2标题", lambda: audit_heads(zones)),
        ("D3表注", lambda: audit_tables_notes(zones, tables)),
        ("D4摘要", lambda: audit_abstracts(zones, paras)),
        ("D5健康", lambda: audit_docx_health(path, zones, paras)),
        ("D6重复", lambda: audit_duplicates(zones)),
        ("D7引用", lambda: audit_citations(zones, path)),
        ("D8字数", lambda: audit_body_chars(zones, ptype, tables)),
    ]
    for name, fn in steps:
        try:
            p, nt = fn()
            problems += p
            notes.update(nt or {})
        except Exception as e:
            problems.append(f"⚠️{name}检查异常:{type(e).__name__}:{str(e)[:40]}")
    verdict = "❌" if any(p.startswith("❌") for p in problems) else ("⚠️" if problems else "✅")
    return {"file": path, "verdict": verdict, "problems": problems, "notes": notes}


TYPE_BY_DIR = {"土木": "土木", "机械": "机械", "经管": "管理", "设计": "设计", "法学": "法学", "管理": "管理"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", required=True)
    ap.add_argument("--root", default="论文终版")
    ap.add_argument("--csv", required=True)
    args = ap.parse_args()
    files = json.load(open(args.list))
    results = []
    for i, rel in enumerate(files):
        parts = rel.replace("\\", "/").split("/")
        category = parts[1] if len(parts) > 1 else ""
        if category not in TYPE_BY_DIR:
            r = {"file": rel, "verdict": "❌", "problems": [f"未知专业目录:{category}"], "notes": {}}
        else:
            ptype = TYPE_BY_DIR[category]
            r = audit_one(os.path.join(args.root, rel), ptype)
        r["file"] = rel
        results.append(r)
        if (i + 1) % 20 == 0:
            print(f"[{i+1}/{len(files)}]", flush=True)
    cnt = Counter(r["verdict"] for r in results)
    print(f"深度审计 {len(results)} 篇: {dict(cnt)}")
    with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["文件", "终审", "问题", "正文数字", "目录行", "文献数", "重复段", "TXT状态", "TXT路径", "TXT硬问题", "TXT警告"])
        for r in results:
            w.writerow([r["file"], r["verdict"], "; ".join(r["problems"]),
                        r["notes"].get("body_chars", ""), r["notes"].get("toc_lines", ""),
                        r["notes"].get("refs", ""), len(r["notes"].get("dup_pairs", [])),
                        r["notes"].get("audit_txt", {}).get("status", ""),
                        r["notes"].get("audit_txt", {}).get("path", ""),
                        "; ".join(r["notes"].get("audit_txt", {}).get("hard", [])),
                        "; ".join(r["notes"].get("audit_txt", {}).get("warn", []))])
    json.dump(results, open(args.csv.replace(".csv", ".json"), "w", encoding="utf-8"), ensure_ascii=False, default=str)
    print("CSV:", args.csv)


if __name__ == "__main__":
    main()
