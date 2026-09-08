# -*- coding: utf-8 -*-
"""Consistency gate for mechanical thesis drawings.

The gate does not decide engineering truth.  It prevents the renderer from
silently mixing incompatible values from different chapters or figure specs.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Tuple


KEYS = (
    "part_material", "functional_hole_diameter", "process_hole_diameter",
    "locator_pin_diameters", "functional_hole_center_distance",
    "process_hole_center_distance", "fixture_dimensions", "clamp_force",
    "clamp_force_required", "clamp_force_design", "clamp_force_capacity",
    "cutting_forces", "fixture_material", "datum_scheme",
)


def _norm(v: Any) -> str:
    return re.sub(r"\s+", "", str(v or "")).replace("φ", "Φ").lower()


def _values(spec: Dict[str, Any], key: str) -> List[str]:
    value = spec.get(key)
    if value in (None, "", [], {}):
        return []
    if isinstance(value, (list, tuple, set)):
        return [_norm(x) for x in value if _norm(x)]
    if isinstance(value, dict):
        return [_norm(value)]
    return [_norm(value)]


def validate_mechanical_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a canonical spec and return a machine-readable report."""
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    for key in KEYS:
        vals = sorted(set(_values(spec, key)))
        # Two different locator diameters are expected for a one-face/two-hole
        # scheme; a list is not a conflict in that field.
        if len(vals) > 1 and key not in ("locator_pin_diameters", "process_hole_diameter"):
            errors.append({"key": key, "values": vals, "message": f"{key} has conflicting values"})
    # A one-face/two-hole datum scheme requires two locator diameters.
    datum = _norm(spec.get("datum_scheme"))
    pins = _values(spec, "locator_pin_diameters")
    if ("一面两孔" in datum or "one_face_two_hole" in datum) and len(pins) != 2:
        errors.append({"key": "locator_pin_diameters", "values": pins, "message": "one-face/two-hole datum requires two locator diameters"})
    # A force diagram must not silently use a different force basis.
    forces = spec.get("cutting_forces")
    if isinstance(forces, dict):
        for name in ("Fc", "Ff", "Fp"):
            if name in forces and not isinstance(forces[name], (int, float)):
                errors.append({"key": f"cutting_forces.{name}", "values": [str(forces[name])], "message": "force must be numeric"})
    if spec.get("source_confidence") == "low":
        warnings.append({"key": "source_confidence", "message": "spec was resolved from conflicting prose"})
    return {"ok": not errors, "errors": errors, "warnings": warnings, "spec": spec}


def extract_mechanical_facts(text: str) -> Dict[str, List[str]]:
    """Extract high-value dimensions/materials/forces for conflict reporting."""
    facts: Dict[str, List[str]] = defaultdict(list)
    patterns = {
        "functional_hole_diameter": r"(?:耳孔|安装孔)[^。；\n]{0,8}?(?:孔径|直径)?[^。；\n]{0,5}?(?:Φ|φ)?\s*(\d+(?:\.\d+)?)\s*mm",
        "process_hole_diameter": r"(?:定位孔|工艺孔|零件孔)(?:的基本尺寸)?[^。；\n]{0,12}?(?:Φ|φ)\s*(\d+(?:\.\d+)?)\s*mm",
        "locator_pin_diameter": r"(?:定位销|圆柱销|菱形销)直径[^。；\n]{0,12}?(?:Φ|φ)?\s*(\d+(?:\.\d+)?)\s*mm",
        "functional_hole_center_distance": r"(?:耳孔|安装孔)[^。；\n]{0,8}?(?:中心距|孔距)[^。；\n]{0,8}?(\d+(?:\.\d+)?)\s*mm",
        "process_hole_center_distance": r"(?:定位孔|工艺孔|两销)[^。；\n]{0,12}?(?:中心距|孔距)[^。；\n]{0,8}?(\d+(?:\.\d+)?)\s*mm",
        "fixture_dimensions": r"(?:夹具体|外形尺寸)[^。；\n]{0,25}?(\d+\s*[×x]\s*\d+\s*[×x]\s*\d+)\s*mm",
        "clamp_force_required": r"(?:最小夹紧力|所需最小夹紧力)[^。；\n]{0,15}?(\d+(?:\.\d+)?)\s*N",
        "clamp_force_design": r"(?:实际设计夹紧力|设计夹紧力|夹紧力取)[^。；\n]{0,15}?(\d+(?:\.\d+)?)\s*N",
        "clamp_force_capacity": r"(?:夹紧能力|轴向夹紧力)[^。；\n]{0,20}?(\d+(?:\.\d+)?)\s*(N|kN)",
        "part_material": r"(?:工件材料|零件材料|毛坯[^。；\n]{0,8}?采用|通常采用)[^。；\n]{0,12}?((?:35钢|45钢|40Cr|ZG\d+))",
    }
    for key, pattern in patterns.items():
        for match in re.finditer(pattern, text, re.I):
            value = _norm(match.group(1))
            if key == "clamp_force_capacity" and match.lastindex and match.lastindex >= 2:
                unit = _norm(match.group(2))
                try:
                    value = str(float(value) * (1000 if unit == "kn" else 1)).rstrip("0").rstrip(".") + "N"
                except ValueError:
                    pass
            context = text[max(0, match.start() - 30):match.start()]
            if key.endswith("center_distance"):
                if "位置度" in context or "公差" in context or float(value) < 1:
                    continue
            if value and value not in facts[key]:
                facts[key].append(value)
    # Force values are often written as Fc=..., Ff=..., Fp=...
    for name in ("Fc", "Ff", "Fp"):
        vals = []
        for m in re.finditer(rf"\b{name}\s*(?:计)?\s*[=:]\s*(\d+(?:\.\d+)?)\s*N", text, re.I):
            v = m.group(1) + "N"
            if v not in vals:
                vals.append(v)
        if vals:
            facts[f"cutting_force_{name}"] = vals
    return dict(facts)


def validate_mechanical_text(text: str) -> Dict[str, Any]:
    """Report prose-level conflicts without guessing which value is correct."""
    facts = extract_mechanical_facts(text)
    conflicts = []
    for key, vals in facts.items():
        if len(vals) > 1:
            conflicts.append({"key": key, "values": vals, "message": "same parameter appears with multiple values"})
    return {"ok": not conflicts, "conflicts": conflicts, "facts": facts}


def gate_mechanical_drawing(spec: Dict[str, Any] | None = None, text: str = "") -> Dict[str, Any]:
    """Combined gate used before automatic drawing generation."""
    spec_report = validate_mechanical_spec(spec or {})
    text_report = validate_mechanical_text(text) if text else {"ok": True, "conflicts": [], "facts": {}}
    spec = spec or {}
    errors = list(spec_report["errors"])
    resolved = set(spec.get("resolved_conflicts") or [])
    suppressed = []
    for conflict in text_report["conflicts"]:
        if conflict.get("key") in resolved:
            suppressed.append(conflict)
        else:
            errors.append(conflict)
    warnings = list(spec_report["warnings"])
    if suppressed:
        warnings.append({
            "key": "resolved_conflicts",
            "message": "explicitly resolved: " + ", ".join(sorted(resolved)),
        })
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "spec": spec_report["spec"],
        "facts": text_report["facts"],
    }


def format_gate_report(report: Dict[str, Any]) -> str:
    """Human-readable report suitable for a pre-render log or UI."""
    lines = ["机械参数一致性闸门：" + ("通过" if report.get("ok") else "阻断")]
    for item in report.get("errors", []):
        key = item.get("key", "unknown")
        vals = ", ".join(map(str, item.get("values", [])))
        lines.append(f"- {key}: {vals}；{item.get('message', '')}")
    for item in report.get("warnings", []):
        lines.append(f"- 警告：{item.get('message', '')}")
    return "\n".join(lines)


def resolve_article_spec(text: str) -> Dict[str, Any] | None:
    """Resolve this known fixture-paper pattern using explicit article rules.

    For unrelated mechanical papers this returns None, so the normal gate still
    blocks until a human or upstream planner supplies a canonical spec.
    """
    if "后钢板弹簧吊耳" not in text or "夹具" not in text:
        return None
    return {
        "functional_hole_diameter": "32",
        "process_hole_diameter": ["18", "12"],
        "locator_pin_diameters": ["18", "12"],
        "functional_hole_center_distance": "120",
        "process_hole_center_distance": "120",
        "fixture_dimensions": "320×200×180",
        "fixture_material": "HT200",
        "part_material": "40Cr",
        "datum_scheme": "一面两孔",
        "clamp_force_required": 1200,
        "clamp_force_design": 1500,
        "clamp_force_capacity": 28200,
        "cutting_forces": {"Fc": 2328, "Ff": 1304, "Fp": 2049},
        "resolved_conflicts": [
            "part_material", "process_hole_diameter", "locator_pin_diameter",
            "process_hole_center_distance", "clamp_force_capacity",
        ],
        "resolution_policy": "semantic_split_then_latest_detailed_calculation_section",
    }
