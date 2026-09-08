# -*- coding: utf-8 -*-
"""
tagguard.py — 确定性标签守卫（无LLM调用）
==========================================
在渲染前对 <chart/> <table/> <drawing/> 标签做：
  1. 拼写修复：<draing>/<drowing>/<taible>/<chrt> 等坏标签名 → 正确名
  2. 闭合修复：以 > 结尾但未自闭合的标签 → 补 />
  3. 属性校验与修复：
     - chart: x/y/legend 数量对齐、y 纯数字校验（非数字 → 降级 table_format）
     - table: rows 污染检测（数字/年份/百分比混入 → 重排进 data）、header/rows/data 对账
     - drawing: 四要素齐全性检查、description 长度警告
  4. 不可修复 → 降级或剔除，并全部记录，绝不静默消失

输出: (修复后文本, 结构化报告dict)
用法:
    from tagguard import audit_and_repair
    text2, report = audit_and_repair(text)
"""
import re

# 已知标签拼写错误映射（LLM 高频错拼）
_TAG_TYPOS = {
    "draing": "drawing", "drowing": "drawing", "drwaing": "drawing",
    "drawin": "drawing", "darwing": "drawing", "drawimg": "drawing",
    "taible": "table", "tablle": "table", "tabel": "table", "tabe": "table",
    "chrt": "chart", "char": "chart", "chatr": "chart", "chrat": "chart",
    "cart": "chart",
    # 变体标签(语义等价,属性名不同): graphic/figure/image用name=,规范化映射到title=
    "graphic": "drawing", "figure": "drawing", "fig": "drawing", "image": "drawing",
}
# 变体标签的属性名映射(变体属性名→drawing标准属性名)
_TAG_ALIAS_ATTRS = {
    "graphic": {"name": "title", "caption": "title", "desc": "description"},
    "figure": {"name": "title", "caption": "title", "desc": "description"},
    "fig": {"name": "title", "caption": "title", "desc": "description"},
    "image": {"name": "title", "alt": "title", "desc": "description"},
}
_VALID_TAGS = {"chart", "table", "drawing"}

_NUM_RE = re.compile(r'^-?\d+(?:\.\d+)?$')
_YEAR_RE = re.compile(r'^(?:19|20)\d{2}(?:年)?$')
_POLLUTE_RE = re.compile(r'^\d+(?:\.\d+)?\s*%?[岁年]?$')

_SPLIT_RE = re.compile(r"[;,，；|｜]+")


def _split_values(text: str) -> list:
    return [t.strip() for t in re.split(r"[;,，；]+", text or "") if t.strip()]


def _strip_dangerous(value: str) -> str:
    """去掉会破坏标签语法与属性解析的字符"""
    v = value.replace('"', "'").replace("”", "'").replace("“", "'")
    v = v.replace("<", "（").replace(">", "）")
    return v.strip()


# ==================== 1. 标签扫描（引号感知） ====================

def _scan_tag_spans(text: str):
    """扫描文本，产出 (start, end, tagname, raw, issues)。
    引号感知：属性值里的 > 与 /> 不会提前终止标签。"""
    spans = []
    i = 0
    n = len(text)
    while i < n:
        lt = text.find("<", i)
        if lt < 0:
            break
        m = re.match(r"<([A-Za-z]+)", text[lt:lt + 12])
        if not m:
            i = lt + 1
            continue
        name = m.group(1).lower()
        if name not in _VALID_TAGS and name not in _TAG_TYPOS:
            i = lt + 1
            continue
        # 引号感知扫描找标签结束
        j = lt + 1 + len(name)
        in_quote = None
        end = None
        missing_gt = False
        while j < n:
            ch = text[j]
            if in_quote:
                if ch == in_quote:
                    in_quote = None
                    # 引号闭合后紧跟 "/" 但无 ">" → 自闭合缺右尖括号
                    # （LLM常见笔误：description="..."/正文 直接接后续内容）
                    k = j + 1
                    while k < n and text[k] in " \t":
                        k += 1
                    if k < n and text[k] == "/":
                        k2 = k + 1
                        while k2 < n and text[k2] in " \t":
                            k2 += 1
                        if k2 >= n or text[k2] != ">":
                            end = (k + 1, True)
                            missing_gt = True
                            break
            elif ch in ('"', "“", "”"):
                in_quote = ch
            elif ch == ">" and not in_quote:
                # 找到 '>'；检查是否自闭合（前一个非空白字符是 '/'）
                k = j - 1
                while k >= lt and text[k] in " \t\r\n":
                    k -= 1
                self_closed = k >= lt and text[k] == "/"
                end = (j + 1, self_closed)
                break
            elif ch == "\n" and j - lt > 2000:
                break  # 超长无闭合，放弃
            j += 1
        if end is None:
            # 行内未闭合：取到行尾
            nl = text.find("\n", lt)
            endpos = nl if nl >= 0 else n
            spans.append((lt, endpos, name, text[lt:endpos], ["未闭合"]))
            i = endpos
            continue
        endpos, self_closed = end
        raw = text[lt:endpos]
        issues = []
        if missing_gt:
            issues.append("缺右尖括号")
        elif not self_closed:
            issues.append("未自闭合")
        if name in _TAG_TYPOS:
            issues.append(f"标签名错拼({name})")
        spans.append((lt, endpos, name, raw, issues))
        i = endpos
    return spans


def _parse_attrs(raw: str) -> dict:
    return dict(re.findall(r'(\w+)=["\u201c\u201d\']([^"\u201c\u201d\']*)["\u201c\u201d\']', raw))


# ==================== 2. 各类型校验器 ====================

# 图表类型别名（中文/常见写法 → 引擎识别名）。引擎只认 bar/line/pie/stacked/gantt/
# table_format/structure/scatter/flow/tree/comparison/trend，其余类型会画出空图。
CHART_TYPE_ALIAS = {
    "柱状图": "bar", "柱形图": "bar", "条形图": "bar", "柱图": "bar", "直方图": "bar",
    "折线图": "line", "线图": "line", "趋势图": "line", "曲线图": "line",
    "饼图": "pie", "饼状图": "pie", "占比图": "pie", "圆形图": "pie",
    "堆叠图": "stacked", "堆积图": "stacked", "堆叠柱状图": "stacked",
    "甘特图": "gantt", "横道图": "gantt", "进度图": "gantt",
    "表格图": "table_format", "文字表": "table_format", "表格": "table_format",
    "结构图": "structure", "架构图": "structure", "组织图": "structure",
    "流程图": "flow", "树图": "tree", "树状图": "tree",
    "散点图": "scatter", "点图": "scatter",
}
_ENGINE_TYPES = {"bar", "line", "pie", "stacked", "gantt", "table_format",
                 "structure", "scatter", "flow", "tree", "comparison", "trend"}


def normalize_chart_type(t: str) -> str:
    """图表类型归一化：中文别名/未知类型 → 引擎识别名（未知默认bar）"""
    t = (t or "bar").strip().lower()
    if t in _ENGINE_TYPES:
        return t
    if t in CHART_TYPE_ALIAS:
        return CHART_TYPE_ALIAS[t]
    # 大小写变体等
    return "bar"


def _validate_chart(attrs: dict, seq: int):
    issues, actions = [], []
    x_labels = _split_values(attrs.get("x", ""))
    y_raw = (attrs.get("y", "") or "").strip()
    chart_type = attrs.get("type", "bar") or "bar"
    # 类型归一化（柱状图→bar 等；未知类型降级bar并记录）
    norm_type = normalize_chart_type(chart_type)
    if norm_type != chart_type.strip().lower():
        known_alias = chart_type.strip().lower() in CHART_TYPE_ALIAS
        issues.append(f"类型\"{chart_type}\"{'→别名' if known_alias else '不支持，降级'}为{norm_type}")
        chart_type = norm_type
        attrs = dict(attrs, type=norm_type)
    if not y_raw:
        return None, ["y属性为空，剔除"], "dropped"
    series = [_split_values(s) for s in re.split(r"[;；]", y_raw) if s.strip()]
    if not series:
        return None, ["y无可解析系列，剔除"], "dropped"

    # ---- gantt：x=任务列表，y=每组(start,duration)，特殊对齐规则 ----
    if chart_type == "gantt":
        for i, g in enumerate(series):
            if len(g) != 2:
                return None, [f"第{i+1}组非start,duration两项，拒绝静默截齐"], "dropped"
        if not x_labels:
            x_labels = [f"任务{i+1}" for i in range(len(series))]
            issues.append("x缺失，按组数补任务名")
        if len(x_labels) != len(series):
            return None, [f"任务数({len(x_labels)})与数据组数({len(series)})不一致，拒绝截齐"], "dropped"
        new_attrs = {
            "id": attrs.get("id", str(seq)),
            "title": attrs.get("title", "") or f"图{seq}",
            "type": "gantt",
            "x": ",".join(x_labels),
            "y": ";".join(",".join(g) for g in series),
        }
        if attrs.get("unit"):
            new_attrs["unit"] = attrs["unit"]
        if attrs.get("data_source"):
            new_attrs["data_source"] = attrs["data_source"]
        return new_attrs, issues, ("repaired" if issues else "ok")

    # ---- 文字型图表类型：y 本来就是文字标签，不做数字校验 ----
    text_types = ("table_format", "structure")
    if chart_type not in text_types:
        non_numeric = any(not _NUM_RE.match(v.replace("%", "").replace(",", ""))
                          for s in series for v in s)
        if non_numeric:
            chart_type = "table_format"
            issues.append("y含非数字，降级为文字表格")
            actions.append("degraded_table_format")

    # x 缺失 → 从系列长度补
    if not x_labels:
        n = max(len(s) for s in series)
        x_labels = [f"类别{i+1}" for i in range(n)]
        issues.append("x缺失，按系列长度自动补类别名")
    # 数量对齐
    n_eff = min(len(x_labels), min(len(s) for s in series))
    if n_eff == 0:
        return None, ["x与y数量无法对齐，剔除"], "dropped"
    if len(x_labels) != n_eff or any(len(s) != n_eff for s in series):
        return None, ["x/系列长度不一致，拒绝静默截齐"], "dropped"
    # legend 对齐
    legend = _split_values(attrs.get("legend", ""))
    if len(series) == 1 and len(legend) > 1:
        legend = legend[:1]
        issues.append("单系列但legend多项，已收敛为第一项")
    if len(series) > 1:
        if not legend:
            legend = [f"系列{i+1}" for i in range(len(series))]
            issues.append("多系列缺legend，自动补系列名")
        elif len(legend) != len(series):
            issues.append(f"legend数({len(legend)})与系列数({len(series)})不一致，已对齐")
            if len(legend) > len(series):
                legend = legend[:len(series)]
            else:
                legend = legend + [f"系列{i+1}" for i in range(len(legend), len(series))]

    new_attrs = {
        "id": attrs.get("id", str(seq)),
        "title": attrs.get("title", "") or f"图{seq}",
        "type": chart_type,
        "x": ",".join(x_labels),
        "y": ";".join(",".join(s) for s in series),
    }
    if legend:
        new_attrs["legend"] = ",".join(legend)
    if attrs.get("unit"):
        new_attrs["unit"] = attrs["unit"]
    if attrs.get("data_source"):
        new_attrs["data_source"] = attrs["data_source"]
    return new_attrs, issues, ("repaired" if issues else "ok")


def _greedy_matrix(tokens: list, ncols: int):
    """乱序token流按'行首为非数字'启发式重组成ncols列矩阵。
    规则：非数字token开新行，其后连续数字补位；数字不足补空串。
    用于LLM把整表压成乱序数字流的情况（对齐失败时的兜底重建）。"""
    def _isnum(t):
        return bool(_POLLUTE_RE.match(t) or _YEAR_RE.match(t))
    rows, i = [], 0
    while i < len(tokens):
        t = tokens[i]
        if _isnum(t) and rows and len(rows[-1]) < ncols:
            rows[-1].append(t)
            i += 1
            continue
        row = [t]
        i += 1
        while len(row) < ncols and i < len(tokens) and _isnum(tokens[i]):
            row.append(tokens[i])
            i += 1
        row += [""] * (ncols - len(row))
        rows.append(row)
    return rows


def _validate_table(attrs: dict, seq: int):
    issues = []
    header_raw = attrs.get("header", "") or attrs.get("headers", "") or ""
    headers = [h.strip() for h in _SPLIT_RE.split(header_raw) if h.strip()]
    rows_raw = attrs.get("rows", "") or ""
    data_raw = attrs.get("data", "") or ""
    row_tokens = [r.strip() for r in _SPLIT_RE.split(rows_raw) if r.strip()]
    data_tokens = [t.strip() for t in _SPLIT_RE.split(data_raw) if t.strip()]

    # data 空占位行清理（"|||;|||;..."这类幻觉空行）
    if data_raw:
        _rows_before = len([r for r in re.split(r"[;；]+", data_raw) if r.strip()])
        kept = [r.strip() for r in re.split(r"[;；]+", data_raw)
                if any(c.strip() for c in re.split(r"[|｜]+", r))]
        if len(kept) < _rows_before:
            issues.append(f"data含{_rows_before - len(kept)}个空占位行，已剔除")
            data_raw = ";".join(kept)
            data_tokens = [t.strip() for t in _SPLIT_RE.split(data_raw) if t.strip()]

    if not headers:
        if not data_tokens and not row_tokens:
            return None, ["header/rows/data全空，剔除"], "dropped"
        headers = ["项目", "内容"]
        issues.append("header缺失，按两列补齐")

    # 【护栏】rows整表压扁识别: rows数恰为列数整数倍、首列像行名、其余格像数据，
    # 且现有data不可用(行数不足压扁矩阵或单元格数错乱) → rows就是压平的整张表，
    # 矩阵重建，混乱data丢弃。data可用时交给下方原有机制，避免误伤。
    _nc0 = len(headers)
    if (_nc0 > 1 and len(row_tokens) >= _nc0 * 2 and len(row_tokens) % _nc0 == 0
            and data_raw):
        _nr0 = len(row_tokens) // _nc0
        _mat0 = [row_tokens[i * _nc0:(i + 1) * _nc0] for i in range(_nr0)]
        _first0 = [r[0] for r in _mat0]
        _rest0 = [c for r in _mat0 for c in r[1:]]
        _dr0 = [r.strip() for r in re.split(r"[;；]+", data_raw) if r.strip()]
        _cells0 = [len([c for c in re.split(r"[|｜]+", r) if c.strip()]) for r in _dr0]
        _numish0 = sum(1 for c in _rest0
                       if _POLLUTE_RE.match(c) or _YEAR_RE.match(c)
                       or '%' in c or ':' in c or '—' in c)
        _data_broken = (len(_dr0) < _nr0
                        or sum(1 for c in _cells0 if c not in (_nc0, _nc0 - 1)) > len(_cells0) / 2)
        if (len(set(_first0)) == len(_first0) and _numish0 >= len(_rest0) * 0.3
                and _data_broken):
            row_tokens = _first0
            data_raw = ";".join("|".join(r[1:]) for r in _mat0)
            data_tokens = [t.strip() for t in _SPLIT_RE.split(data_raw) if t.strip()]
            issues.append(f"rows为压扁整表({_nr0}行×{_nc0}列)，已矩阵重建，残缺data已丢弃")

    # rows 污染：纯数字/年份/百分比出现在 rows
    polluted = [t for t in row_tokens if _POLLUTE_RE.match(t) or _YEAR_RE.match(t)]
    if polluted:
        # 空占位data识别: "|;|;|;" 或 ",,," 这类纯分隔符 → 实际为空
        data_rows = [r.strip() for r in re.split(r"[;；]+", data_raw) if r.strip()]
        data_cells = [c for r in data_rows for c in re.split(r"[|｜]+", r) if c.strip()]
        if not data_cells:
            data_rows = []
            data_raw = ""
        if not data_rows:
            # data为空 → rows里塞的是整张表 → 先试整除重排，失败则行首启发式重建
            ncols = len(headers)
            if ncols > 1 and len(row_tokens) >= ncols * 2 and len(row_tokens) % ncols == 0:
                data_raw = ";".join(
                    "|".join(row_tokens[i:i + ncols])
                    for i in range(0, len(row_tokens), ncols))
                row_tokens, data_tokens = [], []
                issues.append(f"rows被数据污染({len(polluted)}项)，已按{ncols}列重排进data")
            elif ncols > 1 and len(row_tokens) >= ncols * 2:
                matrix = _greedy_matrix(row_tokens, ncols)
                # 缺值格用"—"占位：空串会被下游split丢弃导致列错位
                data_raw = ";".join("|".join((c if c else "—") for c in r) for r in matrix)
                row_tokens, data_tokens = [], []
                ragged = sum(1 for r in matrix if any(c == "" for c in r))
                issues.append(f"rows为乱序数据流({len(row_tokens)}项对不齐{ncols}列)，"
                              f"已按行首启发式重建{len(matrix)}行"
                              f"{'(含'+str(ragged)+'行缺值)' if ragged else ''}，建议核对数值归属")
            else:
                row_tokens = [t for t in row_tokens if t not in polluted]
                issues.append(f"rows含{len(polluted)}项数据污染，已从rows剔除")
        elif all("|" in r or "｜" in r for r in data_rows) and len(data_rows) >= 2:
            # data为多列结构 → rows应等于每行首格；从data重建行名
            first_cells = [re.split(r"[|｜]+", r)[0].strip() for r in data_rows]
            if any(c for c in first_cells) and all(
                    not _POLLUTE_RE.match(c) for c in first_cells if c):
                row_tokens = first_cells
                issues.append(f"rows污染({len(polluted)}项)，已从data首格重建行名")
        else:
            row_tokens = [t for t in row_tokens if t not in polluted]
            issues.append(f"rows含{len(polluted)}项数据污染，已剔除")

    # rows 自身构成完整矩阵 且 data 行名与之无关 → data 为幻觉冗余，忽略
    # 豁免: 行名数与data行数相等(合法配对表, 行名数恰为列数偶数倍时易误判)
    ncols_h = len(headers)
    if (ncols_h > 1 and not polluted and len(row_tokens) >= ncols_h * 2
            and len(row_tokens) % ncols_h == 0 and data_tokens
            and len(row_tokens) != len([r for r in re.split(r"[;；]+", data_raw) if r.strip()])):
        rows_names = [row_tokens[i * ncols_h] for i in range(len(row_tokens) // ncols_h)]
        data_first = [re.split(r"[|｜]+", r.strip())[0].strip()
                      for r in re.split(r"[;；]+", data_raw) if r.strip()]
        if not (set(rows_names) & set(d for d in data_first if d)):
            data_raw, data_tokens = "", []
            issues.append(f"rows已构成{len(rows_names)}行×{ncols_h}列完整表，"
                          f"data行名不匹配视为冗余已忽略")

    if row_tokens and data_tokens:
        ncols_data = max(len(headers) - 1, 1)
        need = len(row_tokens) * ncols_data
        if len(data_tokens) < need * 0.5:
            issues.append(f"data仅{len(data_tokens)}项，不足以填充{len(row_tokens)}行×{ncols_data}列")
    new_attrs = {
        "id": attrs.get("id", str(seq)),
        "title": attrs.get("title", "") or f"表{seq}",
        "header": ",".join(_strip_dangerous(h) for h in headers),
    }
    if row_tokens:
        new_attrs["rows"] = ",".join(_strip_dangerous(r) for r in row_tokens)
    if data_raw:
        # 保留原始行/列结构（|列分隔、;行分隔），只清洗危险字符
        new_attrs["data"] = ";".join(
            _strip_dangerous(r) for r in re.split(r"[;；]+", data_raw) if r.strip())
    if attrs.get("data_source"):
        new_attrs["data_source"] = attrs["data_source"]
    return new_attrs, issues, ("repaired" if issues else "ok")


def _validate_drawing(attrs: dict, seq: int):
    issues = []
    title = attrs.get("title", "")
    desc = attrs.get("description", "")
    dtype = attrs.get("type", "")
    if not title:
        issues.append("title缺失")
        title = f"图{seq}（未命名）"
    if not dtype:
        issues.append("type缺失，默认结构图")
        dtype = "结构图"
    if len(desc) < 50:
        issues.append(f"description过短({len(desc)}字<50)，生图信息可能不足")
    new_attrs = {
        "id": attrs.get("id", str(seq)),
        "type": dtype,
        "title": title,
    }
    if desc:
        new_attrs["description"] = desc
    return new_attrs, issues, ("repaired" if any("缺失" in i for i in issues) else "ok")


# ==================== 3. 序列化与主入口 ====================

def _serialize(tagname: str, attrs: dict) -> str:
    parts = [f"<{tagname}"]
    for k, v in attrs.items():
        parts.append(f'{k}="{_strip_dangerous(str(v))}"')
    parts.append("/>")
    return " ".join(parts)


def fix_placeholder_refs(text: str):
    """修复正文里LLM忘填的字面图/表编号占位（"如图X所示""见表X"）。
    规则：占位引用 → 其后最近标签的序号；其后无标签 → 其前最近标签的序号。
    返回 (修复后文本, 修复次数)"""
    import re as _re
    tag_positions = {"图": [], "表": [], "chart": [], "table": []}
    for m in _re.finditer(r'<(drawing|table)\b', text):
        kind = "图" if m.group(1) == "drawing" else "表"
        tag_positions[kind].append(m.start())
    n_fixed = 0
    pat = _re.compile(r'([如见由]?[图表])[Xx×?？Nn#](?=(所示|中|如下|所示))')

    def _repl(m):
        nonlocal n_fixed
        kind = m.group(1)[-1]  # 图 or 表
        pos = m.start()
        cands = tag_positions.get(kind, [])
        if not cands:
            return m.group(0)
        # 最近的后续标签；没有则最近的前置标签
        after = [p for p in cands if p >= pos]
        target = after[0] if after else cands[-1]
        seq = cands.index(target) + 1
        n_fixed += 1
        return f"{m.group(1)}{seq}"

    text = pat.sub(_repl, text)
    return text, n_fixed


def audit_and_repair(text: str):
    """主入口：校验并修复文本中的所有标签。
    返回 (修复后文本, 报告dict)。已正确且规范的标签保持原样不动。"""
    report = {
        "scanned": {"chart": 0, "table": 0, "drawing": 0},
        "fixed": {"typos": 0, "unclosed": 0, "repaired": 0, "degraded": 0, "dropped": 0},
        "details": {"charts": [], "tables": [], "drawings": []},
    }
    if not text or "<" not in text:
        return text, report

    spans = _scan_tag_spans(text)
    if not spans:
        return text, report

    replacements = []  # (start, end, new_text or None)
    for idx, (start, end, name, raw, scan_issues) in enumerate(spans, 1):
        canonical = _TAG_TYPOS.get(name, name)
        report["scanned"][canonical] += 1
        for si in scan_issues:
            if "错拼" in si:
                report["fixed"]["typos"] += 1
            elif "闭合" in si or "尖括号" in si:
                report["fixed"]["unclosed"] += 1

        attrs = _parse_attrs(raw)
        needs_reserialize = False
        # 变体标签属性名映射(graphic name=→drawing title=),命中即需重序列化
        if name in _TAG_ALIAS_ATTRS:
            for src_key, dst_key in _TAG_ALIAS_ATTRS[name].items():
                if src_key in attrs and dst_key not in attrs:
                    attrs[dst_key] = attrs.pop(src_key)
            needs_reserialize = True
        entry = {"seq": idx, "title": attrs.get("title", "")[:40]}
        try:
            if canonical == "chart":
                new_attrs, issues, action = _validate_chart(attrs, idx)
                entry["kind"] = "chart"
            elif canonical == "table":
                new_attrs, issues, action = _validate_table(attrs, idx)
                entry["kind"] = "table"
            else:
                new_attrs, issues, action = _validate_drawing(attrs, idx)
                entry["kind"] = "drawing"
        except Exception as e:  # 校验器自身异常 → 保留原标签，记录
            entry.update({"action": "validator_error", "issues": [f"校验异常:{e}"]})
            report["details"][entry["kind"] + "s"].append(entry)
            continue

        entry["issues"] = issues
        entry["action"] = action
        # 扫描层问题(缺括号/未自闭合/错拼)必须重序列化修复，即使属性校验全通过
        needs_reserialize = bool(scan_issues) or needs_reserialize
        if action == "dropped":
            replacements.append((start, end, None))
            report["fixed"]["dropped"] += 1
        elif issues or needs_reserialize:
            replacements.append((start, end, _serialize(canonical, new_attrs)))
            if action == "repaired":
                report["fixed"]["repaired"] += 1
            elif "降级" in "".join(issues):
                report["fixed"]["degraded"] += 1
        # 无issues且标签名正确、已自闭合 → 原样保留
        report["details"][entry["kind"] + "s"].append(entry)

    # 从后往前替换，避免偏移失效
    for start, end, new in sorted(replacements, key=lambda x: -x[0]):
        text = text[:start] + (new + "\n" if new else "") + text[end:]

    # 正文引用占位修复（"如图X所示"→实际序号）
    text, n_refs = fix_placeholder_refs(text)
    if n_refs:
        report["fixed"]["refs"] = n_refs
    return text, report


def render_report_text(report: dict) -> str:
    """把报告渲染成一行人类可读摘要（用于进度日志）"""
    s = report["scanned"]; f = report["fixed"]
    if not any(s.values()):
        return ""
    parts = [f"标签守卫: chart {s['chart']} / table {s['table']} / drawing {s['drawing']}"]
    fixes = []
    if f["typos"]: fixes.append(f"错拼{f['typos']}")
    if f.get("refs"): fixes.append(f"引用占位{f['refs']}")
    if f["unclosed"]: fixes.append(f"闭合{f['unclosed']}")
    if f["repaired"]: fixes.append(f"修复{f['repaired']}")
    if f["degraded"]: fixes.append(f"降级{f['degraded']}")
    if f["dropped"]: fixes.append(f"剔除{f['dropped']}")
    if fixes:
        parts.append("→ " + " ".join(fixes))
    return " ".join(parts)


# ==================== 4. 跨章数值一致性守卫（全类型通用） ====================

_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
           "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}
_CN_NUM_R = {v: k for k, v in _CN_NUM.items()}

_UNIT_ALIAS = {"平方米": "m2", "m²": "m2", "㎡": "m2",
               "米": "m", "kN/m²": "kN/m2", "kN/m2": "kN/m2", "kN/㎡": "kN/m2",
               "kPa": "kPa", "天": "d", "日": "d",
               "万元": "w", "万元人民币": "w", "亿元": "e", "元": "y"}

# 各类型参数词典：(参数名, 匹配正则)  数值捕获组为1，单位捕获组为2(可无)
_PARAM_DICT = [
    # ---- 土木 ----
    ("总建筑面积", r"总建筑面积(?:约|为|是|达到)?\s*([\d.,]+)\s*(?:m²|㎡|平方米)(?![/\d])"),
    ("建筑总高度", r"建筑总高度(?:约|为|是|达到)?\s*([\d.,]+)\s*(?:m|米)(?![/\d])"),
    ("标准层层高", r"标准层层高(?:约|为|是)?\s*([\d.,]+)\s*(?:m|米)(?![/\d])"),
    ("地上层数", r"地上(?:共)?\s*([0-9一二三四五六七八九十]{1,3})\s*层"),
    ("地下层数", r"地下(?:共)?\s*([0-9一二三四五六七八九十]{1,3})\s*层"),
    ("地基承载力", r"地基承载力(?:特征值)?(?:约|为|是|修正后取为)?\s*([\d.,]+)\s*kPa"),
    ("基本风压", r"基本风压(?:约|为|是)?\s*([\d.,]+)\s*kN/m[²2㎡]"),
    ("基本雪压", r"基本雪压(?:约|为|是)?\s*([\d.,]+)\s*kN/m[²2㎡]"),
    ("抗震设防烈度", r"抗震设防烈度(?:约|为|是)?\s*([0-9一二三四五六七八九十]{1,2})\s*度"),
    ("设计使用年限", r"设计使用年限(?:约|为|是)?\s*([\d.,]+)\s*年"),
    ("总工期", r"(?:施工总工期|总工期)(?:约|为|是|控制在)?\s*([\d.,]+)\s*(?:天|日)"),
    # ---- 机械 ----
    ("夹紧力", r"(?:实际)?夹紧力(?:约|为|是|达到)?\s*([\d.,]+)\s*(?:N|kN|牛)(?![/\dA-Za-z])"),
    ("主轴转速", r"主轴转速(?:约|为|是)?\s*([\d.,]+)\s*r/min"),
    ("电机功率", r"电机(?:额定)?功率(?:约|为|是)?\s*([\d.,]+)\s*kW"),
    # ---- 设计 ----
    ("衣长", r"衣长(?:约|为|是)?\s*([\d.,]+)\s*cm"),
    ("胸围", r"胸围(?:约|为|是)?\s*([\d.,]+)\s*cm"),
    # ---- 管理/通用 ----
    ("营业收入", r"营业收入(?:额)?(?:约|为|是|达到)?\s*([\d.,]+)\s*(万元|亿元|元)(?![/\d])"),
    ("净利润", r"净利润(?:约|为|是|达到)?\s*([\d.,]+)\s*(万元|亿元|元)(?![/\d])"),
    ("利润总额", r"利润总额(?:约|为|是|达到)?\s*([\d.,]+)\s*(万元|亿元|元)(?![/\d])"),
    ("总资产", r"总资产(?:约|为|是|达到)?\s*([\d.,]+)\s*(万元|亿元|元)(?![/\d])"),
    ("资产负债率", r"资产负债率(?:约|为|是|达到)?\s*([\d.,]+)\s*%?(?![/\d])"),
    ("员工人数", r"(?:员工|职工)(?:总)?(?:人数|总数|人数为|规模)?(?:约|共|为|是|达到)?\s*([\d.,]+)\s*(?:人|名)(?![/\d])"),
    ("注册资本", r"注册资本(?:约|为|是)?\s*([\d.,]+)\s*(万元|亿元|元)(?![/\d])"),
    # ---- 通用后缀（兜底，要求带单位降低误报）----
    ("通用面积", r"([\u4e00-\u9fa5]{2,10}面积)(?:约|为|是|达到)?\s*([\d.,]+)\s*(?:m²|㎡|平方米)(?![/\d])"),
    ("通用高度", r"([\u4e00-\u9fa5]{2,10}高度)(?:约|为|是|达到)?\s*([\d.,]+)\s*(?:m|米)(?![/\d])"),
]

# 时间/语境限定词：带这些前缀的数值属于特定语境，不参与跨章冲突
_QUALIFIER_RE = re.compile(
    r"(\d{4}年|20\d\d[-—~至]20?\d{0,2}年?|近[一二三]年|去年|今年|上年|同期|"
    r"最小|最大|极限|额定|设计取|单件|单体|每间|优化后|改善后|实施后|改造后|调整后|方案[ABab一二二2]|改进前|改进后|"
    r"行业平均|平均水平|标杆|对标|目标值?|预计|计划|理想|参考)(?:的)?$")

# 变动动词：紧邻数值出现说明是变化量，排除自动修正
_DIRECTION_RE = re.compile(
    r"(降至|降为|降至约|升到|升至|升为|增至|减至|回落至|突破|下降到|上升到|"
    r"提高了?|降低了?|增长了?|减少了?|下降了?|上升了?|增幅|降幅)")


def _parse_num(s: str):
    s = s.replace(",", "").rstrip(".")
    if s in _CN_NUM:
        return float(_CN_NUM[s])
    try:
        return float(s)
    except ValueError:
        return None


def _unit_key(u: str) -> str:
    return _UNIT_ALIAS.get(u, u)


def _unit_convert(value: float, unit: str):
    """金额单位归一到元，便于跨单位比较；其他单位返回原值"""
    if unit == "亿元":
        return value * 1e8, "y"
    if unit == "万元":
        return value * 1e4, "y"
    if unit == "元":
        return value, "y"
    return value, _unit_key(unit)


def _to_cn_or_arabic(orig_raw: str, new_value: float) -> str:
    """替换时保持原有数字风格（中文数字/阿拉伯）"""
    stripped = orig_raw.strip()
    if stripped and stripped[0] in _CN_NUM:
        iv = int(new_value)
        return _CN_NUM_R.get(iv, str(iv))
    fmt = f"{new_value:g}"
    return fmt


def check_numeric_consistency(text: str, title: str = "", auto_fix: bool = True):
    """跨章数值一致性检查与修正。
    规则:
      1. 标题事实优先（标题中的层数等与正文冲突 → 正文改为标题值）
      2. 同名同语境参数出现多个值 → 多数值投票（≥2次）修正少数派
      3. 带时间/变动语境的数值不参与修正，仅参与报告
    返回 (修正后文本, report) — report为None表示无任何发现"""
    if not text:
        return text, None

    # ---- 标题事实提取 ----
    title_facts = {}
    if title:
        m = re.search(r"([一二三四五六七八九十\d]{1,3})层", title)
        if m:
            v = _parse_num(m.group(1))
            if v:
                title_facts["地上层数"] = v

    # ---- 扫描全文出现 ----
    occurrences = []  # {name, qualifier, value, unit, span, raw, ch}
    chapter_marks = [(m.start(), m.group(1)) for m in re.finditer(r"第([一二三四五六七八九十\d]+)章", text)]
    for pname, pat in _PARAM_DICT:
        for m in re.finditer(pat, text):
            if pname.startswith("通用"):
                # 通用后缀模式：名称组1、数值组2，单位按类型给默认
                raw_val = m.group(2)
                unit = "平方米" if pname == "通用面积" else "米"
                name = m.group(1)
                val_span = m.start(2), m.end(2)
                name_start = m.start(1)
            else:
                raw_val = m.group(1)
                unit = (m.group(2) if (m.lastindex or 1) >= 2 and m.group(2) else "")
                name = pname
                val_span = m.start(1), m.end(1)
                name_start = m.start(0)
            if not raw_val:
                continue
            v = _parse_num(raw_val)
            if v is None:
                continue
            # 前缀语境限定词
            lookback = text[max(0, name_start - 10):name_start]
            qm = _QUALIFIER_RE.search(lookback)
            qualifier = qm.group(1) if qm else ""
            # 变动动词（名称前或数值前6字符内）
            window = text[max(0, m.start() - 4):m.end()]
            direction = bool(_DIRECTION_RE.search(window))
            ch = ""
            for pos, cnum in chapter_marks:
                if pos <= m.start():
                    ch = cnum
            # 去重：同一位置被词典+通用模式双重匹配时，只保留先到的（词典优先）
            if any(not (val_span[1] <= o["span"][0] or val_span[0] >= o["span"][1])
                   for o in occurrences):
                continue
            occurrences.append(dict(name=name, qualifier=qualifier, value=v,
                                    unit=unit, span=val_span, raw=raw_val,
                                    chapter=ch, direction=direction,
                                    full=m.group(0)))
    if not occurrences:
        return text, None

    # ---- 分组找冲突 ----
    groups = {}
    for occ in occurrences:
        if occ["direction"]:
            continue  # 变动值不参与冲突判定
        key = (occ["name"], occ["qualifier"])
        groups.setdefault(key, []).append(occ)

    conflicts, fixes = [], []
    for (name, qualifier), occs in groups.items():
        by_val = {}
        for o in occs:
            cv, cu = _unit_convert(o["value"], o["unit"])
            by_val.setdefault((round(cv, 4), cu), []).append(o)
        if len(by_val) <= 1:
            continue
        vals_str = " vs ".join(
            f"{v[0]:g}{v[1]}×{len(oo)}" for v, oo in by_val.items())
        conflict = {"name": name, "qualifier": qualifier, "values": vals_str,
                    "chapters": sorted(set(o["chapter"] for o in occs))}
        # ---- 修正决策 ----
        if not auto_fix:
            conflicts.append(conflict)
            continue
        target_val = None
        if name in title_facts and qualifier == "":
            tv, tu = _unit_convert(title_facts[name], "")
            if (round(tv, 4), tu) in by_val:
                target_val = (round(tv, 4), tu)
                conflict["rule"] = "标题事实优先"
        if target_val is None and len(by_val) == 2 and qualifier == "":
            # 多数值投票：出现≥2次的值胜出
            for v, oo in by_val.items():
                if len(oo) >= 2:
                    others = [x for vv, xx in by_val.items() if vv != v for x in xx]
                    if all(len(by_val[(vv2)]) < 2 for vv2 in by_val if vv2 != v):
                        target_val = v
                        conflict["rule"] = f"多数值投票({len(oo)}次胜出)"
                    break
        if target_val is None and qualifier == "":
            # 层级3 强多数票：任意值数下，频次≥2且≥次高一倍（如 1500×8 vs 1200×4 vs 计算值×2）
            ranked = sorted(by_val.items(), key=lambda kv: -len(kv[1]))
            if len(ranked) >= 2 and len(ranked[0][1]) >= 2 and \
                    len(ranked[0][1]) >= 2 * len(ranked[1][1]):
                target_val = ranked[0][0]
                conflict["rule"] = f"强多数投票({len(ranked[0][1])}次)"
        if target_val is None and qualifier == "":
            # 层级4 权威章节值：第1章(工程概况)定义值优先；仅摘要有区分值时其次
            ch1_vals, abs_vals = set(), set()
            for o in occs:
                cv, cu = _unit_convert(o["value"], o["unit"])
                k = (round(cv, 4), cu)
                if o["chapter"] in ("一", "1"):
                    ch1_vals.add(k)
                elif o["chapter"] == "":
                    abs_vals.add(k)
            if len(ch1_vals) == 1:
                av = next(iter(ch1_vals))
                if av in by_val:
                    target_val = av
                    conflict["rule"] = "工程概况定义值"
            elif len(ch1_vals | abs_vals) == 1:
                av = next(iter(ch1_vals | abs_vals))
                if av in by_val:
                    target_val = av
                    conflict["rule"] = "摘要定义值"
        if target_val is None and qualifier == "":
            # 层级5 首次出现优先（1×1平局的兜底：定义处通常在前）
            first = min(occs, key=lambda o: o["span"][0])
            cv, cu = _unit_convert(first["value"], first["unit"])
            target_val = (round(cv, 4), cu)
            conflict["rule"] = "首次出现优先"
        if target_val is not None:
            nfixed = 0
            for v, oo in by_val.items():
                if v == target_val:
                    continue
                for o in oo:
                    # 计算语境豁免：所需/理论/校核等语境的数值是计算结果，不是参数陈述，不改
                    ctx = text[max(0, o["span"][0] - 30):min(len(text), o["span"][1] + 15)]
                    if re.search(r"计算|所需|要求|理论|校核|需要|应为|不得小于|大于等于", ctx):
                        continue
                    tv_num = target_val[0]
                    # 还原到原单位
                    if o["unit"] in ("亿元",) and target_val[1] == "y":
                        tv_num = tv_num / 1e8
                    elif o["unit"] in ("万元",) and target_val[1] == "y":
                        tv_num = tv_num / 1e4
                    repl = _to_cn_or_arabic(o["raw"], tv_num)
                    fixes.append((o["span"][0], o["span"][1], repl,
                                  f"{name}: {o['raw']}→{repl}"))
                    nfixed += 1
            conflict["fixed"] = nfixed
        conflicts.append(conflict)

    if not conflicts:
        return text, {"conflicts": [], "fixed_count": 0, "title_facts": title_facts,
                      "params_scanned": len(groups)}

    # ---- 应用修正（从后往前） ----
    for start, end, repl, why in sorted(fixes, key=lambda x: -x[0]):
        text = text[:start] + repl + text[end:]
        print(f"  [一致性守卫] {why}")

    report = {"conflicts": conflicts,
              "fixed_count": len(fixes),
              "title_facts": title_facts,
              "params_scanned": len(groups)}
    return text, report


def consistency_summary(report: dict) -> str:
    if not report:
        return ""
    n = len(report.get("conflicts", []))
    f = report.get("fixed_count", 0)
    if not n:
        return ""
    return f"一致性守卫: 扫描{report.get('params_scanned', 0)}项参数, 冲突{n}, 自动修正{f}"
