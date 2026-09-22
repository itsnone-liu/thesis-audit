# -*- coding: utf-8 -*-
"""report_render.py — 评审报告渲染层 (2026-09-21, 用户定稿格式)。

职责: 把评审器输出的结构化结果渲染为最终呈现的评语文本, 并强制三条约束:
  ① 自然语言, 无程序痕迹词(实测/检测到/程序判定/预检/内嵌计数等)
  ② 三段式: 【总体评价】【具体评价】【修改意见】
  ③ 不合格不打分(渲染层兜底剥离任何分数残留)

用法:
    from report_render import render_and_save
    text = render_and_save(out, save_dir="/tmp/审核报告")   # out=review_thesis返回值
"""
import os
import re

BAN_WORDS = ("实测", "检测到", "程序判定", "预检", "证据显示", "指标显示",
             "内嵌计数", "命中规则", "audit", "LLM", "prompt",
             "审计条款", "核查项", "条款A", "条款B")
_MARKS = ("【总体评价】", "【具体评价】", "【修改意见】")
_ORDER = ("【总体评价】", "【具体评价】", "【修改意见】")
_MARK_RE = re.compile(r"(【总体评价】|【具体评价】|【修改意见】)")
# 封面问题句: 含"封面"的整句(封面不属于论文内容审核范围, 渲染层兜底剔除)
_COVER_SENT = re.compile(r"[^。；\n]*(?:封面|指导教师姓名|封面信息)[^。；\n]*[。；]?")
# 删句后残留的孤立编号行(如 "5. " / "6）")
_LONE_NUM = re.compile(r"(?m)^\s*(?:\d+[\.、．]?\s*|\d+[）)]\s*)(?=\n|$)")
_SCORE_RESIDUE = re.compile(r"\d{2,3}\s*分")
_SCORE_SENT = re.compile(r"[，,。;；]?(综合)?(评定|评分|得分|成绩)[为]?\s*[0-9]{2,3}\s*分?")
# 清洗"审计条款核查：A数值一致性：xxx"类残留标签, 保留后面的自然说明文字
_AUDIT_PREFIX = re.compile(r"(?:审计条款核查|细节核查记录|细节核查)\s*[:：]?\s*(?:显示|结果)?\s*[:：]?\s*")
_AUDIT_LABEL = re.compile(
    r"([；;。]?)\s*[A-F１-６1-6]\s*(?:数值一致性|图表引用一致性|数量词一致性|"
    r"题目内容匹配|摘要结论呼应|表述规范|称谓统一)\s*[—\-–:：]{1,3}"
)


def sanitize(rep: str, qualified: bool) -> str:
    """禁词清洗 + 审计条款残留标签清洗 + 不合格剥分数。"""
    rep = _AUDIT_PREFIX.sub("", rep)
    rep = _AUDIT_LABEL.sub(lambda m: m.group(1) or "", rep)
    for w in BAN_WORDS:
        rep = rep.replace(w, "")
    if not qualified:
        rep = _SCORE_SENT.sub("", rep)
        rep = _SCORE_RESIDUE.sub("", rep)
    # 清理 "1. 1. xxx" 类重复编号(LLM偶发)
    rep = re.sub(r"(?m)^\s*(\d+)[\.、．]\s*\1[\.、．]?\s*", r"\1. ", rep)
    # 保留换行结构(段落独立成行), 只压缩段内多余空白; 空行压成单换行
    rep = rep.replace("\n ", "\n")
    rep = re.sub(r"\n{2,}", "\n", rep)
    rep = re.sub(r"[^\S\n]{2,}", " ", rep)
    return rep.strip()


def _assemble_fallback(r: dict) -> str:
    """评审报告字段缺失时从分项组装(保证三段永远存在)。"""
    parts = ["【总体评价】" + str(r.get("总体评语", ""))]
    sc = r.get("各项评分") or r.get("分项评分") or {}
    det = "\n".join(f"{k}: {str(v.get('评语', ''))}" for k, v in sc.items() if isinstance(v, dict))
    parts.append("【具体评价】" + det)
    sug = r.get("修改建议") or []
    parts.append("【修改意见】\n" + "\n".join(f"{i+1}. {t}" for i, t in enumerate(map(str, sug))))
    return "\n".join(parts)


def _normalize_sections(rep: str) -> str:
    """兜底①: 强制三段固定顺序【总体评价】→【具体评价】→【修改意见】, 且各标记独立成行。

    用单换行\\n连接段落(sanitize会把\\n\\n压成空格), 段落内保持原样。
    """
    if not rep:
        return rep
    # 拆成 (标记, 内容) 片段; 标记在行首时保留
    chunks = re.split(r"(【总体评价】|【具体评价】|【修改意见】)", rep)
    seg = {}
    cur = None
    for c in chunks:
        if c in _ORDER:
            cur = c
            seg.setdefault(cur, [])
        elif cur is not None:
            seg[cur].append(c)
        elif c.strip():
            seg.setdefault("__head", []).append(c)
    head = "".join(seg.get("__head", [])).strip()
    parts = []
    for m in _ORDER:
        body = "".join(seg.get(m, [])).strip()
        if body:
            parts.append(f"{m}\n{body}")
        elif m in seg:
            parts.append(m)  # 标记存在但内容为空时保留标记, 不丢段
    text = "\n".join(parts)
    return (head + "\n" + text).strip() if head and text else (head or text)


def _strip_cover(rep: str) -> str:
    """兜底②: 删除封面相关句子(封面/封面信息/指导教师姓名, 封面不属于论文内容审核范围)。"""
    if "封面" not in rep and "封面信息" not in rep and "指导教师" not in rep:
        return rep
    out = _COVER_SENT.sub("", rep)
    out = _LONE_NUM.sub("", out)
    return out


def render(out: dict) -> str:
    """review_thesis 返回值 → 最终评语文本(带文件头)。"""
    r = out.get("result", {})
    info = out.get("thesis_info", {}) or {}
    qualified = bool(r.get("是否合格"))
    rep = r.get("评审报告") or ""
    if not all(m in rep for m in _MARKS):
        rep = rep if rep else _assemble_fallback(r)
        # 缺哪段补哪段
        if "【修改意见】" not in rep:
            sug = r.get("修改建议") or []
            rep += "\n\n【修改意见】\n" + ("\n".join(f"{i+1}. {t}" for i, t in enumerate(map(str, sug))) or "无")
        if "【具体评价】" not in rep:
            sc = r.get("各项评分") or r.get("分项评分") or {}
            rep += "\n\n【具体评价】" + "\n".join(
                f"{k}: {str(v.get('评语', ''))}" for k, v in sc.items() if isinstance(v, dict))
        if "【总体评价】" not in rep:
            rep = "【总体评价】" + str(r.get("总体评语", "")) + "\n" + rep
    rep = _normalize_sections(rep)
    rep = _strip_cover(rep)
    rep = sanitize(rep, qualified)
    fn = os.path.basename(out.get("file", "") or "")
    verdict = "合格" if qualified else "不合格"
    header = f"{info.get('title') or ''}（{fn}）— {verdict}"
    if qualified and r.get("总分"):
        header += f"，建议成绩{r.get('总分')}分"
    return header + "\n" + "=" * 40 + "\n" + rep


def render_and_save(out: dict, save_dir: str) -> str:
    text = render(out)
    os.makedirs(save_dir, exist_ok=True)
    fn = os.path.basename(out.get("file", "") or "报告")
    stem = re.sub(r"\.docx?$", "", fn)
    with open(os.path.join(save_dir, f"{stem}_评语.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    return text
