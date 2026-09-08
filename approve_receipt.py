# -*- coding: utf-8 -*-
"""人工批准渲染回执中的降级项 — 占位图/fallback 从"阻断"降为"人工复核"。

用法:
  python3 approve_receipt.py 论文.docx --field placeholder --by 张三 --reason "示意图已人工确认"
  python3 approve_receipt.py 论文.docx --field fallback --by 李四 --reason "表格兜底已核对"

规则:
  - 只写 receipt 的 manual_approvals 字段, 不改 docx, 不改其他回执内容
  - 原子写入(tmp+os.replace), 批准人必填
  - 批准是可审计动作: 时间戳+批准人+理由全量留痕
"""
import argparse
import datetime
import json
import os
import sys


def approve(docx_path: str, field: str, approver: str, reason: str) -> dict:
    receipt_path = docx_path + ".report.json"
    if not os.path.exists(receipt_path):
        raise SystemExit(f"❌ 找不到回执: {receipt_path}")
    with open(receipt_path, "r", encoding="utf-8") as f:
        rc = json.load(f)
    appr = rc.get("manual_approvals")
    if not isinstance(appr, dict):
        appr = {}
    appr[field] = {
        "approved_by": approver,
        "reason": reason or "",
        "approved_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    rc["manual_approvals"] = appr
    tmp = receipt_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rc, f, ensure_ascii=False, indent=1, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, receipt_path)
    return rc


def main():
    ap = argparse.ArgumentParser(description="人工批准渲染回执降级项")
    ap.add_argument("docx", help="论文docx路径(旁车.report.json)")
    ap.add_argument("--field", required=True, choices=["placeholder", "fallback"],
                    help="批准的降级类别")
    ap.add_argument("--by", required=True, help="批准人(必填)")
    ap.add_argument("--reason", default="", help="批准理由")
    args = ap.parse_args()
    rc = approve(args.docx, args.field, args.by, args.reason)
    print(f"✅ 已批准 {args.field} → {args.docx}.report.json")
    print(f"   批准人: {args.by} | 理由: {args.reason or '(未填)'}")
    print(f"   当前回执verdict: {rc.get('verdict')} (audit_docx 将按已批准降级处理)")


if __name__ == "__main__":
    main()
