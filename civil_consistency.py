# -*- coding: utf-8 -*-
"""Parameter consistency gate for civil-engineering thesis figures.

The gate is deliberately conservative: it only compares values when the
article labels them as the same engineering parameter (story height, member
section, slab thickness, etc.).  Unlabelled dimensions are not treated as a
conflict because a civil paper legitimately contains many different lengths.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, List


_NUM = r"(\d+(?:\.\d+)?)"


def _norm(value: Any) -> str:
    s = re.sub(r"\s+", "", str(value or "")).replace("×", "x").lower()
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        s = str(float(s))  # 数值归一: 3.60 == 3.6
    return s


def _add(facts: Dict[str, List[str]], key: str, value: str) -> None:
    value = _norm(value)
    if value and value not in facts[key]:
        facts[key].append(value)


def extract_civil_facts(text: str) -> Dict[str, List[str]]:
    """Extract repeated, semantically-labelled civil design parameters."""
    facts: Dict[str, List[str]] = defaultdict(list)
    patterns = {
        "floor_count": rf"(?:总层数|地上(?:建筑)?层数|共)(?:为|均?约?为?|：|:)?\s*{_NUM}\s*层",
        # 各层层高语义不同(首层/标准层/设备层),分开成独立key才可比较
        "story_height_first": rf"首层层高(?:为|：|:)?\s*{_NUM}\s*m",
        "story_height_standard": rf"(?:标准层高|标准层层高)(?:均?约?为?|：|:)?\s*{_NUM}\s*m",
        "slab_thickness_floor": rf"(?:楼板厚度|楼板厚)(?:为|：|:)?\s*{_NUM}\s*mm",
        "slab_thickness_roof": rf"(?:屋面板厚|屋面板厚度)(?:为|：|:)?\s*{_NUM}\s*mm",
        "column_section": r"(?:柱截面|框架柱截面|柱尺寸)(?:为|：|:)?\s*(\d+(?:\.\d+)?\s*[x×]\s*\d+(?:\.\d+)?)\s*mm",
        "beam_section": r"(?:梁截面|框架梁截面|梁尺寸)(?:为|：|:)?\s*(\d+(?:\.\d+)?\s*[x×]\s*\d+(?:\.\d+)?)\s*mm",
        "concrete_grade": r"(?:混凝土强度等级|混凝土等级|采用混凝土)(?:为|：|:)?\s*(C\d+)",
        "rebar_grade": r"(?:钢筋等级|纵筋等级|箍筋等级|采用钢筋)(?:为|：|:)?\s*(HRB\d+|HPB\d+|HRB\s*\d+)",
        "building_height": rf"(?:建筑高度|结构高度)(?:均?约?为?|：|:)?\s*{_NUM}\s*m",
        # 只认真总工期; "计划工期"是分阶段专用词,分部工程也写"总工期"(如"主体结构总工期120天")
        # → 匹配点前18字内含分部限定词的丢弃
        "construction_duration": rf"(?:本工程|该工程|施工)?总工期(?:为|：|:)?\s*{_NUM}\s*(?:天|日|d)",
    }
    # 工程合理值域: 表格单元格拼接伪影(如"标准层层高 16.35m"实为总高串格)直接丢弃
    PLAUSIBLE = {
        "story_height_first": (2.4, 6.5), "story_height_standard": (2.4, 6.5),
        "building_height": (3.0, 120.0), "construction_duration": (30, 2000),
        "floor_count": (1, 60),
    }
    for key, pattern in patterns.items():
        for match in re.finditer(pattern, text, re.I):
            v = match.group(1)
            if key == "construction_duration":
                prefix = text[max(0, match.start() - 18):match.start()]
                if re.search(r"结构|阶段|基础|装修|机电|安装|室外|单体|栋|层主体|每层|周期|穿插", prefix):
                    continue  # 分部工程工期,非全项目总工期
            if key in PLAUSIBLE:
                try:
                    if not (PLAUSIBLE[key][0] <= float(v) <= PLAUSIBLE[key][1]):
                        continue
                except ValueError:
                    continue
            _add(facts, key, v)

    # Horizontal seismic-force charts often repeat Fi values.  Keep the
    # sequence as one field so it can be compared with a drawing spec.
    seismic = re.findall(r"(?:F\s*[iI]|水平地震作用)[^\d]{0,12}(\d+(?:\.\d+)?)\s*kN", text)
    if seismic:
        facts["seismic_force_values"] = list(dict.fromkeys(_norm(x) for x in seismic))
    return dict(facts)


def validate_civil_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    for key, value in spec.items():
        if key in {"resolved_conflicts", "resolution_policy", "source_confidence"}:
            continue
        values = value if isinstance(value, (list, tuple, set)) else [value]
        normalized = sorted({_norm(v) for v in values if _norm(v)})
        if len(normalized) > 1:
            errors.append({"key": key, "values": normalized, "message": "same civil parameter has conflicting values"})
    if spec.get("source_confidence") == "low":
        warnings.append({"key": "source_confidence", "message": "civil spec was resolved from conflicting prose"})
    return {"ok": not errors, "errors": errors, "warnings": warnings, "spec": spec}


def gate_civil_drawing(spec: Dict[str, Any] | None = None, text: str = "") -> Dict[str, Any]:
    """Gate a complete civil drawing batch before any image is rendered."""
    spec = spec or {}
    spec_report = validate_civil_spec(spec)
    facts = extract_civil_facts(text) if text else {}
    errors = list(spec_report["errors"])
    warnings = list(spec_report["warnings"])
    resolved = set(spec.get("resolved_conflicts") or [])
    # 分部位差异属正常工程实践(垫层C15/主体C30, 主次梁不同截面),只warn不阻断
    PART_SCOPED = {"concrete_grade", "rebar_grade", "beam_section", "column_section",
                   "slab_thickness_floor", "slab_thickness_roof"}
    for key, values in facts.items():
        # A seismic distribution is intentionally a sequence of different Fi
        # values, not a contradiction between two statements.
        if key == "seismic_force_values":
            continue
        if len(values) <= 1:
            continue
        if key in PART_SCOPED:
            warnings.append({"key": key, "values": values,
                             "message": "分部位参数多值(需人工确认部位语义)"})
            continue
        if key in resolved:
            warnings.append({"key": key, "message": "explicitly resolved: " + ", ".join(values)})
        else:
            errors.append({"key": key, "values": values, "message": "same parameter appears with multiple values"})
    return {"ok": not errors, "errors": errors, "warnings": warnings, "spec": spec, "facts": facts}


def format_civil_gate_report(report: Dict[str, Any]) -> str:
    lines = ["土木参数一致性闸门：" + ("通过" if report.get("ok") else "阻断")]
    for item in report.get("errors", []):
        lines.append(f"- {item.get('key', 'unknown')}: {', '.join(map(str, item.get('values', [])))}；{item.get('message', '')}")
    for item in report.get("warnings", []):
        lines.append(f"- 警告：{item.get('message', '')}")
    return "\n".join(lines)
