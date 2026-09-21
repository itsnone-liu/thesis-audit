# -*- coding: utf-8 -*-
"""batch_v2.py — 批量审核系统增强版 (0908增强包)。
相对 批量/batch.py 的变更:
1. major_category_map 新增 机械类/土木类
2. 机械/土木走 ThesisEngReviewer(工科框架+图表硬性+审计原则细化)
3. 其余专业沿用原 lwjg/lwsj/lwfx(需将原 批量/ 目录与本目录同层级放置)
用法: python batch_v2.py <论文文件夹>
"""
import os
import sys
import json
import logging
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../批量"))

from lw_gx_tm import ThesisEngReviewer

MAJOR_MAP = {
    # 经管类(原lwjg)
    "财务管理": "经管类", "人力资源管理": "经管类", "金融学": "经管类", "会计学": "经管类",
    "工商管理": "经管类", "市场营销": "经管类", "旅游管理": "经管类",
    "国际经济与贸易": "经管类", "物流管理": "经管类", "电子商务": "经管类",
    # 艺术设计类(原lwsj)
    "环境设计": "艺术设计类", "视觉传达设计": "艺术设计类", "服装与服饰设计": "艺术设计类",
    "数字媒体艺术": "艺术设计类", "产品设计": "艺术设计类", "艺术设计": "艺术设计类", "动画": "艺术设计类",
    # 法学类(原lwfx)
    "法学": "法学类",
    # v2新增 工科类
    "机械工程": "机械类", "机械设计制造及其自动化": "机械类", "机械设计制造及自动化": "机械类",
    "土木工程": "土木类", "建筑工程技术": "土木类", "建设工程管理": "土木类",
}


def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else input("论文文件夹路径: ").strip()
    if not os.path.isdir(folder):
        print("文件夹不存在"); return
    # 原评审器按需懒加载(保持与原批量目录的相对位置)
    reviewers = {}
    try:
        from lwjg import ThesisDeepSeekReviewer
        reviewers["经管类"] = lambda: ThesisDeepSeekReviewer(api_key=os.environ.get("DEEPSEEK_API_KEY"))
    except ImportError:
        print("⚠ 经管评审器(lwjg)未加载——需 批量/ 目录在上级")
    try:
        from lwsj import ThesisDesignReviewer
        reviewers["艺术设计类"] = lambda: ThesisDesignReviewer(api_key=os.environ.get("DEEPSEEK_API_KEY"))
    except ImportError:
        print("⚠ 设计评审器(lwsj)未加载")
    try:
        from lwfx import ThesisLawReviewer
        reviewers["法学类"] = lambda: ThesisLawReviewer(api_key=os.environ.get("DEEPSEEK_API_KEY"))
    except ImportError:
        print("⚠ 法学评审器(lwfx)未加载")
    reviewers["机械类"] = lambda: ThesisEngReviewer(api_key=os.environ.get("DEEPSEEK_API_KEY"), etype="机械")
    reviewers["土木类"] = lambda: ThesisEngReviewer(api_key=os.environ.get("DEEPSEEK_API_KEY"), etype="土木")

    # 从文件名猜专业(学号_姓名_专业 或封面提取; 与原batch一致可换)
    from docx import Document
    import re
    results = []
    files = sorted(f for f in os.listdir(folder) if f.endswith(".docx") and not f.startswith("~"))
    for f in files:
        path = os.path.join(folder, f)
        major = None
        try:
            doc = Document(path)
            for p in doc.paragraphs[:60]:
                t = p.text.strip()
                m = re.match(r"专\s*业[:：]\s*(\S+)", t)
                if m:
                    major = m.group(1)
                    break
        except Exception:
            pass
        cat = MAJOR_MAP.get(major or "", None)
        if not cat:
            # 文件名后缀猜
            for k in MAJOR_MAP:
                if k in f:
                    cat = MAJOR_MAP[k]; break
        if not cat:
            results.append({"file": f, "major": major, "error": "专业未识别/不在支持范围"})
            print(f"跳过(专业未识别): {f}")
            continue
        rv = reviewers[cat]()
        try:
            out = rv.review_thesis(path)
            # 2026-09-21: 统一报告渲染(自然语言三段式, 不合格不打分)
            import report_render as RR
            rep_text = RR.render_and_save(out, os.path.join(folder, "评语"))
            results.append({"file": f, "major": major, "category": cat,
                            "合格": out["result"].get("是否合格"),
                            "总分": out["result"].get("是否合格") and out["result"].get("总分") or None,
                            "评语文件": f"评语/{os.path.splitext(f)[0]}_评语.txt",
                            "stats": out["thesis_info"].get("stats", {})})
            print(f"{'✅' if out['result'].get('是否合格') else '❌'} {f[:28]} [{cat}] "
                  f"{out['result'].get('总分')}分")
        except Exception as e:
            results.append({"file": f, "major": major, "error": str(e)[:100]})
            print(f"⚠ {f}: {type(e).__name__}")
    outf = os.path.join(folder, f"批量审核_v2_{datetime.now():%m%d_%H%M}.json")
    json.dump(results, open(outf, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    ok = sum(1 for r in results if r.get("合格"))
    print(f"\n完成: {ok}/{len(results)} 合格 → {outf}")


if __name__ == "__main__":
    main()
