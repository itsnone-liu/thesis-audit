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
             "内嵌计数", "命中规则", "audit", "LLM", "prompt")
_MARKS = ("【总体评价】", "【具体评价】", "【修改意见】")
_SCORE_RESIDUE = re.compile(r"\d{2,3}\s*分")
_SCORE_SENT = re.compile(r"[，,。;；]?(综合)?(评定|评分|得分|成绩)[为]?\s*[0-9]{2,3}\s*分?")


def sanitize(rep: str, qualified: bool) -> str:
    """禁词清洗 + 不合格剥分数。"""
    for w in BAN_WORDS:
        rep = rep.replace(w, "")
    if not qualified:
        rep = _SCORE_SENT.sub("", rep)
        rep = _SCORE_RESIDUE.sub("", rep)
    return re.sub(r"\s{2,}", " ", rep.replace("\n ", "\n")).strip()


def _assemble_fallback(r: dict) -> str:
    """评审报告字段缺失时从分项组装(保证三段永远存在)。"""
    parts = ["【总体评价】" + str(r.get("总体评语", ""))]
    sc = r.get("各项评分") or r.get("分项评分") or {}
    det = "\n".join(f"{k}: {str(v.get('评语', ''))}" for k, v in sc.items() if isinstance(v, dict))
    parts.append("【具体评价】" + det)
    sug = r.get("修改建议") or []
    parts.append("【修改意见】\n" + "\n".join(f"{i+1}. {t}" for i, t in enumerate(map(str, sug))))
    return "\n".join(parts)


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
