# -*- coding: utf-8 -*-
"""audit_principles.py — 审计原则细化共享模块 (0908增强包)。
源自 273 篇全量审计体系(audit_deep/audit_final)沉淀的确定性检查原则。
用途: ①给评审prompt注入实测证据(防LLM幻觉) ②审计细化条款文本块。
"""
import re
import zipfile
from docx import Document

# ============ 审计原则细化条款(追加到各专业评审prompt) ============
PRINCIPLES_TEXT = """
## 审计细化条款(在原有要求之上, 逐项核查并写入评语)
A. 数值一致性: 摘要/正文/结论中同一指标的数值必须一致(如占比、金额、面积、高度、产量)。
   "从a增长至b"类表述的方向词必须与数字大小关系相符(升配升、降配降)。
B. 图表引用一致性: 每张图/表必须有图注/表注(如"图3-1 xxx"), 且正文中必须有对应引用
   ("如图3-1所示"/"见表4-2")。引用了不存在的编号、或有图无注、有注无引用, 均为缺陷。
C. 数量词一致性: "三大问题/四个模块/五大维度"类数量表述在摘要、正文、结论中必须统一,
   且与实际展开的小节数量相符。
D. 题目-内容匹配: 研究对象名称、地域、主体在题目/摘要/正文/结论中必须一致;
   研究对象的脱敏表述("XX公司""某高校""某村""A公司""B集团"等)是盲审常规形态, 视为有效研究对象,
   本身不构成任何缺陷, 不得据此扣分或判不合格; 真缺陷是错位(题目对象与正文对象不一致)
   或全文混用真实名称与脱敏名造成指代不清。
E. 摘要-结论呼应: 结论必须逐一回应摘要提出的研究问题, 不得引入摘要未提的新结论主体。
F. 表述规范: 同一对象全文称谓统一(全称/简称不混用造成歧义)。
"""


# ============ 确定性预检(实测证据) ============
def _doc_plain(path):
    z = zipfile.ZipFile(path)
    txt = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>",
                 z.read("word/document.xml").decode("utf-8", "replace")))
    return txt


def count_images(path):
    """内嵌图计数: 按命名空间本地名数 drawing/pict(不依赖前缀——自定义前缀如ns4:也计)。
    不用media文件数(docx会复用media部件)。"""
    import xml.etree.ElementTree as ET
    z = zipfile.ZipFile(path)
    root = ET.fromstring(z.read("word/document.xml"))
    n = 0
    for el in root.iter():
        tag = el.tag.rsplit('}', 1)[-1] if '}' in el.tag else el.tag
        if tag in ('drawing', 'pict'):
            n += 1
    return n


def caption_ref_audit(path):
    """图注/表注 vs 正文引用 对账。返回问题列表。"""
    doc = Document(path)
    paras = [p.text.strip() for p in doc.paragraphs]
    CAPT = re.compile(r"^([图表])\s*(\d{1,2})(?:\s*[-–—]\s*(\d{1,2}))?\s+\S")
    VERBY = re.compile(r"显示|如下|所示|可知|可以看出|中可|给出|列出|反映了|表明")
    REF = re.compile(r"[如见]?([图表])(\d{1,2})(?:[-–—](\d{1,2}))?")
    problems = []
    for kind in ("图", "表"):
        caps = set()
        for s in paras:
            if len(s) > 60 or _VERBY_FIRST(s, VERBY):
                continue
            m = CAPT.match(s)
            if m and m.group(1) == kind:
                key = (int(m.group(2)), int(m.group(3)) if m.group(3) else None)
                caps.add(key)
        refs = set()
        for s in paras:
            for m in REF.finditer(s):
                if m.group(1) == kind:
                    refs.add((int(m.group(2)), int(m.group(3)) if m.group(3) else None))
        bad = [r for r in refs if r not in caps]
        unreferenced = [c for c in caps if c not in refs]
        if bad:
            problems.append(f"{kind}引用失配{len(bad)}处: " + ",".join(
                f"{kind}{c}-{n}" if n else f"{kind}{c}" for c, n in bad[:5]))
        if unreferenced:
            problems.append(f"{kind}注未被正文引用{len(unreferenced)}处")
    return problems


def _VERBY_FIRST(s, VERBY):
    return bool(VERBY.search(s[:20]))


def numeric_contradiction_candidates(path):
    """摘要vs正文的数字差集(候选矛盾, 供LLM复核)。返回列表[(num, where)]。"""
    txt = _doc_plain(path)
    # 摘要区粗定位: '摘 要'/'摘要' 到第一个'关键词'
    m = re.search(r"摘\s*要", txt)
    k = re.search(r"关键词", txt)
    abstract = txt[m.end():k.start()] if (m and k and k.start() > m.end()) else ""
    body = txt[k.end():] if k else txt
    if not abstract:
        return []
    def nums_of(seg):
        out = {}
        for mm in re.finditer(r"(\d+\.\d+|\d{3,5}|\d{1,3})\s*%?", seg):
            v = mm.group(0).strip()
            if re.match(r"^(19|20)\d{2}$", v):
                continue  # 年份跳过
            out[v] = out.get(v, 0) + 1
        return out
    an, bn = nums_of(abstract), nums_of(body)
    # 摘要独有数字(正文0次) → 可能是矛盾点(也可能正常)
    suspicious = [v for v, c in an.items() if v not in bn and len(v.rstrip('%')) >= 2]
    return suspicious[:10]


def placeholder_check(path):
    """脱敏/代称检测(观察项): XX公司/某某/某村——按盲审口径为有效对象形态, 非缺陷"""
    txt = _doc_plain(path)
    hits = re.findall(r"XX[A-Za-z\u4e00-\u9fff]{0,6}|某某[\u4e00-\u9fff]{0,4}|某村|某公司(?![的])", txt)
    return [h for h in hits if h][:10]


def precheck(path):
    """汇总预检 → 注入prompt的实测证据dict"""
    try:
        doc = Document(path)
        w = sum(len(p.text) for p in doc.paragraphs)
        tb = len(doc.tables)
    except Exception:
        w, tb = 0, 0
    return {
        "word_count": w,
        "table_count": tb,
        "image_count": count_images(path),
        "caption_ref_issues": caption_ref_audit(path),
        "numeric_candidates": numeric_contradiction_candidates(path),
        "placeholders": placeholder_check(path),
    }


def evidence_block(pc):
    """预检结果 → prompt文本块"""
    lines = [f"- 实测总字数≈{pc['word_count']}字; 表格{pc['table_count']}个; 图片{pc['image_count']}张(内嵌计数)"]
    if pc["caption_ref_issues"]:
        lines.append(f"- ⚠️图表引用对账问题: {'; '.join(pc['caption_ref_issues'][:6])}")
    else:
        lines.append("- 图表引用对账: 未发现失配(引用号与图注号匹配)")
    if pc["numeric_candidates"]:
        lines.append(f"- ⚠️摘要独有数字(正文未再现, 疑似数值不一致, 请核对是否矛盾): {pc['numeric_candidates']}")
    if pc["placeholders"]:
        lines.append(f"- 观察项·脱敏/代称出现: {pc['placeholders']}"
                     "(脱敏是有效研究对象形态, 非缺陷; 仅当与真实名称混用致指代不清时在条款D下报告)")
    else:
        lines.append("- 未检出脱敏/代称表述")
    return "\n".join(lines)
