# -*- coding: utf-8 -*-
""" Vendored 自 thesis-reviser/core.py — 仅含 audit_docx.py 需要的三个宽容提取函数
(tolerant_extract_charts / tolerant_extract_tables / extract_drawings_from_text)
及其内部helper。本仓库是纯审核工具链, 不含渲染管线; 完整core见
https://github.com/itsnone-liu/thesis-reviser2
同步纪律: 若上游core.py这些函数变更, 需同步本文件(vendored, 行号514-%d)。 """
import re

def tolerant_extract_charts(text):
    """宽容解析<chart/>标签 — 支持属性跨行和中文引号"""
    result = []
    matches = list(re.finditer(r'<chart\b.*?/>', text, flags=re.DOTALL))
    if matches:
        for m in matches:
            tag = m.group()
            attrs = dict(re.findall(r'(\w+)=["\u201c\u201d\']([^"\u201c\u201d\']*)["\u201c\u201d\']', tag))
            if attrs:
                result.append({
                    "id": attrs.get("id", ""),
                    "title": attrs.get("title", ""),
                    "type": attrs.get("type", ""),
                    "x": attrs.get("x", ""),
                    "y": attrs.get("y", ""),
                    "legend": attrs.get("legend", ""),
                    "unit": attrs.get("unit", ""),
                    "data_source": attrs.get("data_source", attrs.get("datasource", "")),
                    "start_pos": m.start(), "end_pos": m.end()
                })
        return result

    for m in re.finditer(r'<chart\s+.*?(?:/>|>|(?=\n))', text, re.DOTALL):
        tag = m.group()
        start_pos = m.start()
        end_pos = m.end()
        attrs = dict(re.findall(r'(\w+)=["\u201c\u201d\']([^"\u201c\u201d\']*)["\u201c\u201d\']', tag))

        if tag.rstrip().endswith('/>'):
            remaining = text[end_pos:]
            next_lines = remaining.split('\n', 8)[:8]
            for line in next_lines:
                stripped = line.strip()
                kv_match = re.match(r'(\w+)=["\u201c\u201d]?(.*?)["\u201c\u201d]?\s*(?:/)?\s*$', stripped)
                if kv_match and kv_match.group(1) in ('type', 'x', 'y', 'legend', 'unit', 'data_source', 'id', 'title'):
                    key = kv_match.group(1)
                    val = kv_match.group(2).rstrip('/').rstrip()
                    if key not in attrs or not attrs[key]:
                        attrs[key] = val
                    end_pos = text.index(line, end_pos) + len(line)
                else:
                    break

        if attrs:
            result.append({
                "id": attrs.get("id", ""),
                "title": attrs.get("title", ""),
                "type": attrs.get("type", ""),
                "x": attrs.get("x", ""),
                "y": attrs.get("y", ""),
                "legend": attrs.get("legend", ""),
                "unit": attrs.get("unit", ""),
                "data_source": attrs.get("data_source", attrs.get("datasource", "")),
                "start_pos": start_pos, "end_pos": end_pos
            })
    return result


def tolerant_extract_tables(text):
    """宽容解析<table/>标签 — 不修改原文本，支持属性跨行"""
    result = []
    # 优先解析完整的自闭合 table 标签，支持属性跨行
    matches = list(re.finditer(r'<table\b.*?/>', text, flags=re.DOTALL))
    if matches:
        for m in matches:
            tag = m.group()
            attrs = dict(re.findall(r'(\w+)=["\u201c\u201d\']([^"\u201c\u201d\']*)["\u201c\u201d\']', tag))
            if 'header' not in attrs and 'headers' in attrs:
                attrs['header'] = attrs['headers']
            if attrs and ('title' in attrs or 'header' in attrs or 'rows' in attrs):
                result.append({
                    "id": attrs.get("id", ""),
                    "title": attrs.get("title", ""),
                    "header": attrs.get("header", ""),
                    "rows": attrs.get("rows", ""),
                    "data": attrs.get("data", ""),
                    "data_source": attrs.get("data_source", attrs.get("datasource", "")),
                    "start_pos": m.start(), "end_pos": m.end()
                })
        return result

    # 兼容旧版不完整输出：逐行扫描首行，再尝试吸收后续属性
    for m in re.finditer(r'<table\s+[^\n]*', text):
        tag = m.group()
        end_pos = m.end()
        start_pos = m.start()

        attrs = dict(re.findall(r'(\w+)=["\u201c\u201d\']([^"\u201c\u201d\']*)["\u201c\u201d\']', tag))
        if 'header' not in attrs and 'headers' in attrs:
            attrs['header'] = attrs['headers']

        remaining = text[end_pos:]
        next_lines = remaining.split('\n', 6)[:6]
        for line in next_lines:
            stripped = line.strip()
            kv_match = re.match(r'(\w+)=["\u201c\u201d]?(.*?)["\u201c\u201d]?\s*(?:/)?\s*$', stripped)
            if kv_match and kv_match.group(1) in ('header', 'rows', 'data', 'data_source', 'id', 'title'):
                key = kv_match.group(1)
                val = kv_match.group(2).rstrip('/').rstrip()
                if key not in attrs or not attrs[key]:
                    attrs[key] = val
                end_pos = text.index(line, end_pos) + len(line)
            else:
                break

        if attrs and ('title' in attrs or 'header' in attrs or 'rows' in attrs):
            result.append({
                "id": attrs.get("id", ""),
                "title": attrs.get("title", ""),
                "header": attrs.get("header", ""),
                "rows": attrs.get("rows", ""),
                "data": attrs.get("data", ""),
                "data_source": attrs.get("data_source", attrs.get("datasource", "")),
                "start_pos": start_pos, "end_pos": end_pos
            })
    return result


def _infer_drawing_type(text: str, fallback: str = "结构图") -> str:
    """根据标题/描述推断图纸类型。"""
    t = (text or "").replace(" ", "")
    rules = [
        ("运动过程图", ["运动过程", "动作过程", "动作时序", "流程", "工艺流程"]),
        ("装配示意图", ["装配示意", "装配关系", "装配图"]),
        ("受力分析图", ["受力分析", "受力", "力学分析", "载荷", "应力"]),
        ("原理图", ["原理图", "工作原理", "原理"]),
        ("总体布局图", ["总体布局", "布局图", "总布置", "总体方案"]),
        ("结构图", ["结构图", "结构示意", "结构分解", "结构"]),
        ("零件图", ["零件图", "零件结构", "零件"]),
        ("传动简图", ["传动简图", "传动链", "传动"]),
        ("流程图", ["流程图", "工艺流程", "流程"]),
        ("安装布局图", ["安装布局", "安装示意", "安装"]),
    ]
    for typ, kws in rules:
        if any(k in t for k in kws):
            return typ
    return fallback


def _is_placeholder_drawing_text(text: str) -> bool:
    """过滤明显的占位文本，避免把半成品 drawing 标签渲染进 DOCX。"""
    raw = (text or "").strip()
    if not raw:
        return True
    compact = re.sub(r'[\s\W_]+', '', raw, flags=re.UNICODE)
    if compact in {"", ">", "<", "/", "图", "图纸", "示意", "概览", "原图", "示意图", "结构图", "设计图", "工程图", "机械图"}:
        return True
    if raw in {">", "<", "/", "／", ">", ">>", "<<", "—", "-", "｜", "|"}:
        return True
    return len(compact) <= 1


def _prefer_structured_drawing_value(base_value: Any, structured_value: Any) -> Any:
    """当基础值明显是占位符时，用结构化值覆盖。"""
    if structured_value in (None, ""):
        return base_value
    if base_value in (None, ""):
        return structured_value
    if _is_placeholder_drawing_text(str(base_value)):
        return structured_value
    return base_value


def _canonicalize_drawing_line(line: str, seq: int = 0,
                               start_pos: int = 0, end_pos: int = 0) -> Optional[dict]:
    """
    解析单行 drawing 标签：
    - 支持严格格式
    - 支持 `<drawing/ 图1-1 xxx，yyy` 之类的半成品格式
    - 返回统一字典，供渲染阶段按顺序编号
    """
    raw = (line or "").strip()
    if "<drawing" not in raw.lower():
        return None

    strict = re.search(
        r'<drawing\s+id="([^"]+)"(?:\s+type="([^"]*)")?\s+title="([^"]+)"(?:\s+description="([^"]*)")?\s*/?>',
        raw, re.DOTALL
    )
    if strict:
        strict_title = strict.group(3).strip()
        strict_desc = (strict.group(4) or "").strip()
        if _is_placeholder_drawing_text(strict_title) and _is_placeholder_drawing_text(strict_desc):
            return None
        if _is_placeholder_drawing_text(strict_title) and strict_desc and not _is_placeholder_drawing_text(strict_desc):
            strict_title = strict_desc
        attrs = {
            "id": strict.group(1),
            "type": strict.group(2) or "",
            "title": strict_title,
            "description": strict_desc,
        }
        attrs["seq"] = seq or 0
        attrs["start_pos"] = start_pos
        attrs["end_pos"] = end_pos
        return attrs

    attrs = dict(re.findall(r'(\w+)=["\']([^"\']*)["\']', raw))
    tail = raw
    # 去掉前缀与多余闭合符
    tail = re.sub(r'(?is)^.*?<drawing\s*/*\s*', '', tail)
    tail = tail.replace('<drawing', '').strip()
    tail = re.sub(r'/\s*>?\s*$', '', tail).strip()
    tail = re.sub(r'^[图图]\s*[\d一二三四五六七八九十]+(?:[-.]\d+)?[：:、\s-]*', '', tail)
    tail = tail.strip(' /')
    if attrs:
        title = attrs.get("title", "").strip()
        desc = attrs.get("description", "").strip()
        typ = attrs.get("type", "").strip()
        if _is_placeholder_drawing_text(title) and _is_placeholder_drawing_text(desc):
            return None
        if _is_placeholder_drawing_text(title) and desc and not _is_placeholder_drawing_text(desc):
            title = desc
        if not title:
            # 只有 description 时，优先把 description 作为标题，而不是整段原始文本
            if desc:
                title = desc
            else:
                # 兜底：从原始文本提取一个较短标题
                parts = [p.strip() for p in re.split(r'[，,；;。]', tail) if p.strip()]
                title = parts[0] if parts else tail[:24]
        if not desc:
            parts = [p.strip() for p in re.split(r'[，,；;。]', tail) if p.strip()]
            desc = '；'.join(parts[1:]) if len(parts) > 1 else (parts[0] if parts else tail)
        if not typ:
            typ = _infer_drawing_type(f"{title} {desc}")
        return {
            "id": attrs.get("id", str(seq or 0)),
            "type": typ,
            "title": title,
            "description": desc,
            "seq": seq or 0,
            "start_pos": start_pos,
            "end_pos": end_pos,
        }

    if not tail:
        return None
    parts = [p.strip() for p in re.split(r'[，,；;。]', tail) if p.strip()]
    title = parts[0] if parts else tail[:24]
    desc = '；'.join(parts[1:]) if len(parts) > 1 else (parts[0] if parts else tail)
    if _is_placeholder_drawing_text(title) and _is_placeholder_drawing_text(desc):
        return None
    if len(title) < 3 and desc:
        title = desc[:18]
    typ = _infer_drawing_type(f"{title} {desc}")
    return {
        "id": str(seq or 0),
        "type": typ,
        "title": title,
        "description": desc,
        "seq": seq or 0,
        "start_pos": start_pos,
        "end_pos": end_pos,
    }


def _normalize_drawing_signature(drawing: dict) -> str:
    """为 drawing 生成内容签名，用于识别重复图纸标签。"""
    if not isinstance(drawing, dict):
        return ""
    pieces = []
    for key in ("type", "title", "description", "scene", "layout"):
        val = drawing.get(key)
        text = re.sub(r"\s+", "", str(val or ""))
        if text:
            pieces.append(text)
    return "|".join(pieces)


def tolerant_extract_drawings(text):
    """宽容解析<drawing/>标签 — 不修改原文本"""
    return extract_drawings_from_text(text)


def extract_charts(text: str) -> list:
    """解析 <chart/> 标签，返回列表 [{"id":..., "title":..., "type":..., "x":..., "y":..., "unit":..., "data_source":..., "start_pos":..., "end_pos":...}]"""
    charts = []
    for m in re.finditer(r'<chart\b.*?/>', text, flags=re.DOTALL | re.I):
        attrs = dict(re.findall(r'(\w+)\s*=\s*["\']([^"\']*)["\']', m.group()))
        if not attrs:
            continue
        if not all(attrs.get(k) for k in ("id", "title", "type", "x", "y")):
            continue
        charts.append({
            "id": attrs.get("id", ""),
            "title": attrs.get("title", ""),
            "type": attrs.get("type", ""),
            "x": attrs.get("x", ""),
            "y": attrs.get("y", ""),
            "legend": attrs.get("legend", ""),
            "unit": attrs.get("unit", ""),
            "data_source": attrs.get("data_source", ""),
            "start_pos": m.start(), "end_pos": m.end()
        })
    return charts


def extract_tables(text: str) -> list:
    """解析 <table/> 标签，返回列表（容忍LLM未闭合标签）"""
    tables = []
    for m in re.finditer(r'<table\b.*?/>', text, flags=re.DOTALL | re.I):
        attrs = dict(re.findall(r'(\w+)\s*=\s*["\']([^"\']*)["\']', m.group()))
        # 兼容 LLM 输出 header/headers 两种写法
        if 'header' not in attrs and 'headers' in attrs:
            attrs['header'] = attrs['headers']
        if not all(attrs.get(k) for k in ("id", "title", "header", "rows", "data")):
            continue
        tables.append({
            "id": attrs.get("id", ""),
            "title": attrs.get("title", ""),
            "header": attrs.get("header", ""),
            "rows": attrs.get("rows", ""),
            "data": attrs.get("data", ""),
            "data_source": attrs.get("data_source", "") or attrs.get("datasource", ""),
            "start_pos": m.start(), "end_pos": m.end()
        })
    return tables


def extract_drawings_from_text(text: str) -> list:
    """从文本中提取所有 <drawing/> 标签"""
    drawings = []
    seen_signatures = set()
    seq = 0

    # 优先处理完整的跨行 <drawing ... /> 标签
    matches = list(re.finditer(r'<drawing\b.*?/>', text, flags=re.DOTALL))
    if matches:
        for m in matches:
            raw = m.group()
            parsed = _canonicalize_drawing_line(raw, seq + 1, m.start(), m.end())
            if parsed:
                sig = _normalize_drawing_signature(parsed)
                if sig and sig in seen_signatures:
                    continue
                if sig:
                    seen_signatures.add(sig)
                seq += 1
                drawings.append(parsed)
        if drawings:
            return drawings

    pos = 0
    for line in text.splitlines(True):
        line_end = pos + len(line)
        if "<drawing" in line.lower():
            parsed = _canonicalize_drawing_line(line, seq + 1, pos, line_end)
            if parsed:
                sig = _normalize_drawing_signature(parsed)
                if sig and sig in seen_signatures:
                    pos = line_end
                    continue
                if sig:
                    seen_signatures.add(sig)
                seq += 1
                drawings.append(parsed)
        pos = line_end
    return drawings
