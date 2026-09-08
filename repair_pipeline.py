# -*- coding: utf-8 -*-
"""返修管线 (2026-09-07/08) — 83篇确证硬伤+低匹配论文修复。

策略(题目/关键词锁定不动):
  A. 数值矛盾 → 口径统一: 统计矛盾数字变体在正文区出现次数, 以正文主体口径
     为基准, 把摘要/结论区的少数派数字替换为基准值(run-safe 替换, 保留格式)。
  B. 数量/模块矛盾(四vs五) → 以正文实际展开结构为准, 改摘要/结论表述。
  C. 对象错位(低匹配) → 绪论加口径框定段(LLM生成, 程序插入), 正文个别措辞调整。
  D. 算术错误(76.4取整73) → 以算术正确方为基准统一。

流程: 审计JSON读取矛盾 → 决策(程序规则优先, 复杂篇LLM) → 副本应用编辑
     → 复审(确定性重跑 + 数字矛盾消解验证, 语义篇LLM重审) → 不引入新问题
     → 另存 论文终版_返修0908/ + 返修记录_0908.csv

用法: python3 repair_pipeline.py [--limit N] [--only 文件名片段]
"""
import os, re, sys, csv, json, shutil, argparse, zipfile
from collections import Counter
from docx import Document

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import audit_deep as AD
import audit_final as AF
from audit_llm import call_llm, parse_json, extract_material

ROOT = os.path.join(BASE, "论文终版")
OUT_ROOT = os.path.join(BASE, "论文终版_返修0908")
LIST_F = os.path.join(BASE, "论文修订档案/_返修清单_0908.json")
DEEP_F = os.path.join(BASE, "论文修订档案/审计报告_0908深度.json")
LLM_F = os.path.join(BASE, "论文修订档案/审计LLM_0908.json")
REC_F = os.path.join(BASE, "论文修订档案/返修记录_0908.csv")
STATE_F = os.path.join(BASE, "论文修订档案/_返修state_0908.json")


# ---------------------------------------------------------------- run-safe 替换
def replace_in_paragraph(p, old, new) -> bool:
    """段内替换 old→new, 跨 run 安全: 重建段落文本到首个 run, 其余 run 清空。
    仅当 old 真的在段内才动, 保持段落主格式。"""
    full = p.text
    if old not in full:
        return False
    newfull = full.replace(old, new)
    runs = p.runs
    if not runs:
        return False
    runs[0].text = newfull
    for r in runs[1:]:
        r.text = ""
    return True


def replace_variants(old, new):
    """生成数字变体对: 全角%/半角%、'数字 %'带空格。"""
    vs = [(old, new)]
    if "%" in old:
        vs.append((old.replace("%", "％"), new.replace("%", "％")))
        vs.append((old.replace("%", " %"), new.replace("%", " %")))
    if "％" in old:
        vs.append((old.replace("％", "%"), new.replace("％", "%")))
        vs.append((old.replace("％", " ％"), new.replace("％", " ％")))
    return vs


def replace_in_doc(doc, old, new, zones_hint=None) -> int:
    n = 0
    for o, nw in replace_variants(old, new):
        for p in doc.paragraphs:
            if o in p.text:
                n += replace_in_paragraph(p, o, nw)
        for t in doc.tables:
            for row in t.rows:
                for c in row.cells:
                    for p in c.paragraphs:
                        if o in p.text:
                            n += replace_in_paragraph(p, o, nw)
    return n


def doc_full_text(path) -> str:
    z = zipfile.ZipFile(path)
    txt = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>",
                             z.read("word/document.xml").decode("utf-8", "replace")))
    return txt


def num_count(txt, n) -> int:
    """数字边界计数(防'70'命中'170')。"""
    n = n.rstrip("%")
    total = 0
    for m in re.finditer(re.escape(n), txt):
        s, e = m.start(), m.end()
        if (s == 0 or not (txt[s-1].isdigit() or txt[s-1] in ".,，")) and \
           (e >= len(txt) or not (txt[e].isdigit() or txt[e] in ".,，")):
            total += 1
    return total


# ---------------------------------------------------------------- 决策: 数量词/方向词
CN_NUM = {"一":1,"二":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,"十":10,"两":2}


def cn2int(s):
    if s.isdigit():
        return int(s)
    return CN_NUM.get(s)


def decide_quantifier_fixes(flaws, txt, body_text=None):
    """中文数量词矛盾: '三项核心问题' vs '四个核心问题'。
    基准优先级: ①flaw提及章的实际小节数(X.1..X.k计数) ②全文字样多数派。"""
    plans = []
    for fl in flaws:
        pairs = []
        for m in re.finditer(r"([一二三四五六七八九十两\d])(?:大|个|项)(?:核心)?(问题|模块|维度|策略|要素|环节|因素|动因|板块|渠道|举措|对策|方面)", fl):
            pairs.append((m.group(1), m.group(2), m.group(0)))
        if len(pairs) < 2:
            continue
        scored = []
        for num, noun, phr in pairs:
            n = cn2int(num)
            if n is None:
                continue
            cnt = len(re.findall(re.escape(phr), txt))
            alt = str(n) if not num.isdigit() else \
                  next((k for k, v in CN_NUM.items() if v == n and k != "两"), None)
            if alt and alt != num:
                for q in ("大", "个", "项"):
                    cnt += len(re.findall(rf"{alt}{q}(?:核心)?{noun}", txt))
            scored.append({"num": n, "noun": noun, "src_num": num, "cnt": cnt})
        uniq = {}
        for s in scored:
            uniq[s["num"]] = s
        if len(uniq) < 2:
            continue
        ranked = sorted(uniq.values(), key=lambda x: -x["cnt"])
        # ① 结构验证: flaw提及"第N章"时数该章 N.k 小节数, 候选与之一致者升为基准
        if body_text:
            mch = re.search(r"第(\d{1,2})章", fl)
            if mch:
                ch = mch.group(1)
                subn = len(set(re.findall(rf"(?<![.\d])({ch}\.\d{{1,2}})(?![.\d])", body_text)))
                if 2 <= subn <= 9:
                    hit = [s for s in ranked if s["num"] == subn]
                    if hit and hit[0] is not ranked[0]:
                        ranked.remove(hit[0])
                        ranked.insert(0, hit[0])
        base, minor = ranked[0], ranked[1:]
        if base["cnt"] == 0:
            continue
        for s in minor:
            for q in ("大", "个", "项"):
                for core in ("核心", ""):
                    for ns, nb in ((str(s["num"]), str(base["num"])),
                                   (next((k for k,v in CN_NUM.items() if v==s["num"] and k!="两"), ""), 
                                    next((k for k,v in CN_NUM.items() if v==base["num"] and k!="两"), ""))):
                        if not ns or not nb or ns == nb:
                            continue
                        old = f"{ns}{q}{core}{s['noun']}"
                        new = f"{nb}{q}{core}{s['noun']}"
                        if old in txt and old != new:
                            plans.append({"old": old, "new": new, "why": fl[:60],
                                          "old_count": s["cnt"], "base_count": base["cnt"]})
    seen, out = set(), []
    for pl in plans:
        if pl["old"] in seen:
            continue
        seen.add(pl["old"])
        out.append(pl)
    return out


def decide_direction_fixes(flaws):
    """方向词矛盾: '从a下降至b'但b>a → 方向词改'增长至'。仅当flaw明确指矛盾。"""
    plans = []
    for fl in flaws:
        if not re.search(r"下降|降低|回落|减少", fl) or \
           not re.search(r"却|但|反而|高于|上升|矛盾|不一致", fl):
            continue
        nums = re.findall(r"(\d+(?:\.\d+)?)", fl)
        vals = [float(n) for n in nums if float(n) >= 1]
        if len(vals) >= 2 and vals[1] > vals[0]:
            for w in ("下降至", "降低至", "回落至", "减少至"):
                if w in fl:
                    plans.append({"old": w, "new": "增长至", "why": fl[:60], "old_count": 1, "base_count": 1})
                    break
    return plans[:1]


# ---------------------------------------------------------------- 决策: 数值统一
YEAR_RE = re.compile(r"^(19|20)\d{2}$")
UNIT_RE = re.compile(r"^(%|％|亿元|万元|元|万人|人|万件|件|米|平方米|平米|万平方米|层|倍|r/min|转/分|kN|kW|mm|MPa|岁|家|个点)$")


def extract_typed_nums(fl):
    """从flaw文本提取带类型的数字: (value_str, unit, domain_key)。
    domain: pct(百分比) / 计数(人件层家…) / 量纲(米mm kN…) / plain。
    年份(19xx/20xx纯整数)与1-20小整数(章节号/图号)一律排除。"""
    out = []
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(%|％|亿元|万元|万人|万件|万平方米|平方米|平米|米|r/min|转/分|kN|kW|MPa|mm|万人|人|件|层|倍|岁|家)", fl):
        v, u = m.group(1), m.group(2)
        if YEAR_RE.match(v):
            continue
        out.append((v, u, f"unit:{u}"))
    # 无单位数字: 3位+整数或带小数, 排除年份/小整数
    for m in re.finditer(r"(?<![\d.])(\d{3,5}|\d+\.\d+)(?![\d.%])", fl):
        v = m.group(1)
        if YEAR_RE.match(v) or (v.isdigit() and int(v) <= 20):
            continue
        # 量级桶: 10^floor(log10)
        mag = len(v.split(".")[0])
        out.append((v, "", f"mag:{mag}"))
    return out


def decide_numeric_fixes(rel, flaws, txt):
    """每条确证矛盾 → 替换对列表 [(old, new, why)]。
    硬约束(宁漏勿错): ①年份永不替换 ②只在同一类型域(同单位/同量级)内配对
    ③old与new量级差<10× ④出现次数少的一方→多的一方(正文主体口径为基准)。"""
    plans = []
    for fl in flaws:
        nums = extract_typed_nums(fl)
        # 按 domain 分组, 组内去重
        domains = {}
        for v, u, dk in nums:
            domains.setdefault(dk, [])
            if (v, u) not in [(x[0], x[1]) for x in domains[dk]]:
                domains[dk].append((v, u))
        for dk, items in domains.items():
            if len(items) < 2:
                continue
            scored = []
            for v, u in items:
                scored.append((v, u, num_count(txt, v)))
            scored.sort(key=lambda x: -x[2])
            (bv, bu, bc) = scored[0]
            for v, u, c in scored[1:]:
                if c == 0:
                    continue
                # 量级检查: 数值差不超过10倍(4.5% vs 2024这类直接出局——domain已隔离, 双保险)
                try:
                    if float(v) and abs(float(v) / float(bv)) > 10 or abs(float(bv) / float(v)) > 10:
                        continue
                except (ValueError, ZeroDivisionError):
                    continue
                # 带单位的替换带单位形态; 无单位的裸数字替换
                old_tok = v + u if u else v
                new_tok = bv + bu if bu else bv
                if c < bc:
                    plans.append({"old": old_tok, "new": new_tok, "why": fl[:70], "old_count": c, "base_count": bc})
    seen, out = set(), []
    for pl in plans:
        if pl["old"] in seen or pl["new"] == pl["old"] or pl["old"] == pl["new"]:
            continue
        seen.add(pl["old"])
        out.append(pl)
    return out


# ---------------------------------------------------------------- 决策: 语义修复(LLM)
def semantic_fix_plan(rel, lr, extra_flaws=None):
    """低匹配/对象错位篇: LLM 产出措辞级修复指令。
    附原文矛盾句上下文, 要求 find 逐字摘自原文。"""
    mat = extract_material(os.path.join(ROOT, rel))
    all_flaws = (lr.get("hard_flaws") or []) + (extra_flaws or [])
    # 从全文抓矛盾句原文(以flaw里的数字/关键词定位)
    try:
        ftxt = doc_full_text(os.path.join(ROOT, rel))
    except Exception:
        ftxt = ""
    ctx_lines = []
    for fl in all_flaws[:4]:
        key = re.search(r"[\u4e00-\u9fff]{6,}", fl)
        kw = key.group(0)[:8] if key else fl[:8]
        i = ftxt.find(kw[:6])
        if i >= 0:
            ctx_lines.append(ftxt[max(0, i-20):i+90])
    prompt = f"""你是论文修复专家。论文题目与关键词绝对不能改动(学位系统对账), 只能调整正文表述使内容自洽。

【题目(不可改)】{mat['title']}
【摘要】{mat['abstract']}
【审计发现的问题】{json.dumps(all_flaws[:5], ensure_ascii=False)}
【问题相关的原文段落(修复时find必须从这些原文或摘要中逐字摘取, 禁止自己编写)】
{chr(10).join(ctx_lines[:4]) or '(无)'}

要求: 最小改动消除上述矛盾(统一口径/方向词/数量词/对象框定), 不改题目关键词, 不重写全文, 修改后的句子必须通顺完整。
只输出JSON(无其他文字):
{{
 "replacements": [{{"find": "原文中不超过50字的连续片段(逐字摘自原文)", "replace": "改后的完整片段"}}],
 "framing": "若题目与研究对象存在错位, 给一句60字内的口径框定句(放绪论, 说明研究对象与题目口径的关系); 无需则空串",
 "rationale": "一句话"
}}"""
    ok, txt = call_llm(prompt, timeout=120)
    if not ok:  # 百炼败 → codex-proxy 兜底
        ok, txt = call_llm(prompt, use_proxy=True, timeout=120)
    if not ok:
        return None, f"LLM调用失败:{txt[:60]}"
    parsed = parse_json(txt)
    if not parsed:
        return None, "JSON解析失败"
    return parsed, None


# ---------------------------------------------------------------- 修后LLM复审
from audit_llm import build_prompt

def llm_reverify(rel_dst):
    """修后单篇LLM重审(同审计口径)。返回 (parsed|None, err)"""
    try:
        mat = extract_material(rel_dst)
    except Exception as e:
        return None, f"素材提取失败:{type(e).__name__}"
    prompt = build_prompt(mat)
    ok, txt = call_llm(prompt, timeout=120)
    if not ok:
        ok, txt = call_llm(prompt, use_proxy=True, timeout=120)
    if not ok:
        return None, f"LLM调用失败:{txt[:50]}"
    parsed = parse_json(txt)
    if not parsed:
        return None, "JSON解析失败"
    return parsed, None


# ---------------------------------------------------------------- 单篇修复
def repair_one(rel, deep_r, lr, do_semantic, extra_flaws=None, low_match_hint=False):
    src = os.path.join(ROOT, rel)
    dst = os.path.join(OUT_ROOT, rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    log = {"file": rel, "actions": [], "errors": []}
    flaws = list(lr.get("hard_flaws") or []) + list(extra_flaws or [])
    # 存疑线索也修(数字复核拦的可能是幻觉, 但原文若有对应数字仍值得统一)
    txt = doc_full_text(src)

    doc = Document(src)
    # --- C 语义修复先行(find基于原文件素材, 先应用防数值改写后失配) ---
    if do_semantic:
        plan, err = semantic_fix_plan(rel, lr, extra_flaws=extra_flaws)
        if err:
            log["errors"].append(f"语义修复失败:{err}")
        else:
            import difflib
            for rp in plan.get("replacements", [])[:8]:
                if rp.get("find") and len(rp["find"]) >= 6:
                    n = replace_in_doc(doc, rp["find"], rp["replace"])
                    if n == 0:
                        # 模糊兜底: 数值修复可能已改写了find所在句 → 找最相近段落片段
                        best, bestp, bscore = None, None, 0.0
                        for p in doc.paragraphs:
                            t = p.text
                            if not t or len(t) < 12 or abs(len(t) - len(rp["find"])) > 220:
                                continue
                            sc = difflib.SequenceMatcher(None, rp["find"], t[:len(rp["find"]) + 60]).ratio()
                            if sc > bscore:
                                best, bestp, bscore = t, p, sc
                        if bestp is not None and bscore > 0.72:
                            replace_in_paragraph(bestp, best, rp["replace"])
                            n = 1
                            log["actions"].append(f"措辞(模糊): '{best[:20]}…'→")
                    log["actions"].append(f"措辞: '{rp['find'][:20]}…'→ ×{n}")
                    if n == 0:
                        log["errors"].append(f"find未命中: {rp['find'][:30]}")
            # 低匹配篇: 绪论第1章标题段之后插入口径框定段(插标题后, 防被划入目录区)
            if low_match_hint and plan.get("framing"):
                from docx.text.paragraph import Paragraph
                from docx.oxml import OxmlElement
                allparas = [p.text for p in doc.paragraphs]
                zn = AD.split_zones(allparas)
                b0 = zn["toc_range"][1]
                for p in doc.paragraphs[b0:b0+40]:
                    if AD.CH_HEAD.match(p.text.strip()) and not AD._toc_line(p.text.strip()):
                        np_ = OxmlElement("w:p")
                        newp = Paragraph(np_, p._parent)
                        newp.style = p.style
                        newp.add_run(plan["framing"])
                        p._p.addnext(np_)  # 紧跟第1章标题段之后
                        log["actions"].append(f"绪论框定段(章后): {plan['framing'][:32]}…")
                        break
    # --- A/D 数值统一 + 数量词 + 方向词(在语义修复之后) ---
    plans = decide_numeric_fixes(rel, flaws, txt)
    plans += decide_quantifier_fixes(flaws, txt, body_text=txt)
    plans += decide_direction_fixes(flaws)
    for pl in plans:
        n = replace_in_doc(doc, pl["old"], pl["new"])
        if n:
            log["actions"].append(f"统一: {pl['old']}→{pl['new']} ×{n} ({pl['why'][:36]})")
    doc.save(dst)
    return log


# ---------------------------------------------------------------- 复审
def reverify(rel, log):
    """修后复审: ①确定性重跑, 与修前比不允许新增问题 ②原矛盾数字对消解。
    返回 (ok, detail)。"""
    dst = os.path.join(OUT_ROOT, rel)
    _cat = rel.split("/")[1]
    ptype = {"土木": "土木", "机械": "机械", "经管": "管理", "设计": "设计", "法学": "法学", "管理": "管理"}.get(_cat)
    if not ptype:
        return False, f"未知专业目录:{_cat}，复审无法执行"
    r2 = AD.audit_one(dst, ptype)
    old_probs = set(p.split(":")[0][:10] for p in log.get("prev_problems", []))
    new_hard = [p for p in r2["problems"] if p.startswith("❌")]
    if new_hard:
        return False, f"复审新❌: {new_hard[:2]}"
    # 原数值矛盾消解检查: 替换后的旧数字不应再出现(除非它本来就是常见数如年份)
    ntxt = doc_full_text(dst)
    for pl in log.get("numeric_plans", []):
        resid = 0
        for o, _ in replace_variants(pl["old"], pl["old"]):
            # 带单位完整形态搜索(num_count会strip单位导致'4.5倍'误判'4.5%'残留)
            if "%" in o or "％" in o:
                resid += ntxt.count(o)
            else:
                resid += num_count(ntxt, o)
        if resid > 0 and pl["old"] not in ("2020", "2021", "2022", "2023", "2024", "2025"):
            return False, f"旧数字{pl['old']}仍残留{resid}处"
    return True, f"确定性通过; 修复{len(log['actions'])}项"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default="")
    ap.add_argument("--list", dest="listfile", default="", help="替代返修清单json路径")
    ap.add_argument("--llm", dest="llmfile", default="", help="替代LLM审计json路径")
    ap.add_argument("--deep", dest="deepfile", default="", help="替代深度审计json路径(csv)")
    ap.add_argument("--state", dest="statefile", default="", help="替代state文件路径")
    ap.add_argument("--rec", dest="recfile", default="", help="替代返修记录csv路径")
    ap.add_argument("--llm-review", action="store_true", help="语义修复篇修后LLM重审")
    args = ap.parse_args()
    files = json.load(open(args.listfile or LIST_F))
    llm_f = args.llmfile or LLM_F
    deep_f = args.deepfile or DEEP_F
    state_f = args.statefile or STATE_F
    rec_f = args.recfile or REC_F
    if args.only:
        files = [f for f in files if args.only in f]
    if args.limit:
        files = files[:args.limit]
    if deep_f.endswith(".csv"):
        import csv as _csv
        deep = {r["文件"]: {"problems": (r.get("问题") or "").split(";")} for r in _csv.DictReader(open(deep_f, encoding="utf-8-sig"))}
    else:
        deep = {r["file"]: r for r in json.load(open(deep_f, encoding="utf-8"))}
    llm = json.load(open(llm_f, encoding="utf-8"))
    state = {}
    if os.path.exists(state_f):
        state = json.load(open(state_f, encoding="utf-8"))
    records = []
    for i, rel in enumerate(files):
        if state.get(rel, {}).get("done"):
            records.append(state[rel])
            continue
        lr = llm.get(rel, {})
        deep_r = deep.get(rel, {})
        low_match = isinstance(lr.get("topic_match"), (int, float)) and lr["topic_match"] < 7
        try:
            extra_flaws = []
            for attempt in range(3):  # 修→审→不达标带新flaw重修, 最多3轮
                log = repair_one(rel, deep_r, lr, do_semantic=low_match or attempt > 0,
                                 extra_flaws=extra_flaws, low_match_hint=low_match)
                log["prev_problems"] = deep_r.get("problems", [])
                txt = doc_full_text(os.path.join(ROOT, rel))
                log["numeric_plans"] = decide_numeric_fixes(rel, (lr.get("hard_flaws") or []) + extra_flaws, txt)
                ok, detail = reverify(rel, log)
                log["done"], log["verify"] = ok, detail
                if not ok:
                    break
                # LLM复审: hard_flaws应为空; 语义篇还要求topic_match>=7
                parsed, err = llm_reverify(os.path.join(OUT_ROOT, rel))
                if parsed is None:
                    log["verify"] += f"; LLM复审跳过({err})"
                    break
                log["llm_re"] = {"verdict": parsed.get("verdict"), "topic_match": parsed.get("topic_match"), "summary": parsed.get("summary", "")}
                new_hard = [f for f in (parsed.get("hard_flaws") or []) if f]
                tm = parsed.get("topic_match")
                need_tm = low_match and isinstance(tm, (int, float)) and tm < 7
                if not new_hard and not need_tm:
                    break
                extra_flaws = new_hard
                log["verify"] += f"; LLM复审仍有问题(第{attempt+1}轮): {[f[:40] for f in new_hard[:2]]}{(' topic=%s' % tm) if need_tm else ''}"
                if attempt == 2:
                    log["done"] = False
                    log["errors"].append("3轮仍未达标: " + "; ".join(f[:50] for f in new_hard[:2]))
        except Exception as e:
            log = {"file": rel, "done": False, "verify": f"异常:{type(e).__name__}:{str(e)[:60]}", "actions": [], "errors": [str(e)[:80]]}
        state[rel] = log
        records.append(log)
        if (i + 1) % 10 == 0:
            json.dump(state, open(state_f, "w", encoding="utf-8"), ensure_ascii=False)
            done_n = sum(1 for v in state.values() if v.get("done"))
            print(f"[{i+1}/{len(files)}] 复审通过 {done_n}", flush=True)
    json.dump(state, open(state_f, "w", encoding="utf-8"), ensure_ascii=False)
    with open(rec_f, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["文件", "复审", "修复动作", "问题", "验证详情"])
        for r in records:
            w.writerow([r["file"], "✅" if r.get("done") else "❌",
                        " | ".join(r.get("actions", [])), "; ".join(r.get("errors", [])),
                        r.get("verify", "")])
    ok_n = sum(1 for r in records if r.get("done"))
    print(f"返修完成: {ok_n}/{len(records)} 复审通过 → {rec_f}")


if __name__ == "__main__":
    main()
