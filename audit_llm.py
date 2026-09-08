# -*- coding: utf-8 -*-
"""LLM 语义审计层 (2026-09-07) — 无源论文语义质量复审。

【证据范围】每条记录 evidence_scope=片段: 只看题目+摘要+各章开头+结论开头+前6条文献,
不是全文通读; 本层任何"pass"都只表示"片段未见问题", 不得当作全文终审结论。

每篇一次打包调用, 输入: 封面题目 + 中文摘要 + 章节标题 + 各章首段 + 结论首段 +
参考文献前6条; 判定:
  1) 题目-内容匹配   论文实际做的是否是题目说的事
  2) 跑题章节        明显与题目无关的章
  3) 摘要质量        是否真实概括(空话套话/与正文不符)
  4) 结论呼应        结论是否回应题目与摘要
  5) 参考文献异常    格式混乱/年份异常/明显凑数
  6) 硬伤抽查        数据前后矛盾/常识错误(能从给定片段看出的)

模型路由: 百炼 deepseek-v4-flash 优先(快省); 429/1308 或连续失败 → codex-proxy
gpt-5.6-luna(OpenAI兼容, 本机8788). 断点续跑(_state json).

用法: python3 audit_llm.py --list 论文修订档案/_无源清单_0908.json \
        --out 论文修订档案/审计LLM_0908.json [--root 论文终版] [--limit N]
"""
import os, re, sys, csv, json, time, argparse, random
import urllib.request
from docx import Document

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audit_deep as AD
import audit_final as AF

STATE = {"dashscope_fail": 0, "proxy_fail": 0}

# ---- key 加载(不入日志不入报告) ----
def load_keys():
    keys = {}
    _here = os.path.dirname(os.path.abspath(__file__))
    for envf in (os.path.join(_here, ".env"),
                 "/root/project/workspace/thesis-reviser/.env", "/etc/codex-proxy.env", "/root/.hermes/.env"):
        try:
            for ln in open(envf):
                m = re.match(r"^([A-Z_]+)=(.+)$", ln.strip())
                if m and m.group(2).strip("'\""):
                    keys.setdefault(m.group(1), m.group(2).strip("'\""))
        except OSError:
            pass
    return keys

KEYS = load_keys()
DS_KEY = KEYS.get("DASHSCOPE_API_KEY", os.environ.get("DASHSCOPE_API_KEY", ""))
CP_KEY = KEYS.get("CODEX_PROXY_API_KEY", os.environ.get("CODEX_PROXY_API_KEY", ""))
DS_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
CP_URL = "http://127.0.0.1:8788/v1/chat/completions"


def call_llm(prompt, use_proxy=False, timeout=90):
    """返回 (ok, text_or_err)."""
    if use_proxy:
        url, key, model = CP_URL, CP_KEY, "gpt-5.6-luna"
    else:
        url, key, model = DS_URL, DS_KEY, "deepseek-v4-flash-0731"
    if not key:
        return False, "no-key"
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 700,
        **({"enable_thinking": False} if not use_proxy else {}),
    }).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
        txt = data["choices"][0]["message"].get("content") or ""
        return bool(txt.strip()), txt
    except urllib.error.HTTPError as e:
        detail = e.read()[:200].decode("utf-8", "replace")
        return False, f"HTTP{e.code}:{detail}"
    except Exception as e:
        return False, f"{type(e).__name__}:{str(e)[:80]}"


# ---------------------------------------------------------------- 素材提取
def extract_material(path):
    d = Document(path)
    paras = [p.text.strip() for p in d.paragraphs]
    zones = AD.split_zones([p.text for p in d.paragraphs])
    # 题目: 封面"论文题目：xxx" 或首行含"题目"
    title = ""
    for t in paras[:16]:
        m = re.match(r"^论文题目[:：]\s*(.+)$", t) or re.match(r"^题\s*目[:：]\s*(.+)$", t)
        if m:
            title = m.group(1).strip()
            break
    # 摘要: 摘要标题后的第一段长文本
    abstract = ""
    for i, t in enumerate(paras[:60]):
        if re.match(r"^摘\s*要$", t) and i + 1 < len(paras):
            abstract = paras[i + 1][:500]
            break
    # 章标题 + 每章首段
    chapters = []  # (head, first_para)
    cur_head, buf = None, []
    for t in zones["body"]:
        if not t:
            continue
        if AD.CH_HEAD.match(t) and not AD._toc_line(t):
            if cur_head and buf:
                chapters.append((cur_head, " ".join(buf)[:220]))
            cur_head, buf = t, []
        elif cur_head is not None:
            if len(t) > 30:
                buf.append(t)
                if len(buf) >= 2:
                    chapters.append((cur_head, " ".join(buf)[:220]))
                    cur_head, buf = None, []
            continue
    if cur_head and buf:
        chapters.append((cur_head, " ".join(buf)[:220]))
    # 结论章(最后一个含 结论/总结 的章)首段
    concl = ""
    for h, fp in chapters:
        if re.search(r"结论|总结", h):
            concl = f"{h} — {fp[:200]}"
    # 参考文献
    refs = [t for t in zones["refs"] if re.match(r"^\[\d+\]", t)][:6]
    return {
        "title": title, "abstract": abstract,
        "chapters": chapters[:10], "conclusion": concl, "refs": refs,
    }


PROMPT_TMPL = """你是成人高等教育(专升本)毕业论文的评审专家。以下是一篇论文的关键片段(各章仅开头, 非全文), 请严格但克制地评审。

【重要约束】你只看到片段: "片段中未见XX"不等于论文没有XX。只报告**片段内可见的确定矛盾**(如摘要与正文数值不一致、行内算术错误、自相矛盾的表述); "缺少依据/未见计算"类仅当出现在摘要或结论的**关键宣称**上时才列为弱提示(放weak_notes), 不得据此判fail。

【题目】{title}
【中文摘要】{abstract}
【章节结构与各章开头】
{chapters}
【结论章开头】{conclusion}
【参考文献(前6条)】
{refs}

请只输出一个JSON对象(不要其他文字), 字段:
{{
 "topic_match": 0-10,          // 题目与实际内容的匹配度(10=完全一致)
 "offtopic": ["章节名:一句原因"], // 明显与题目无关的章, 没有则[]
 "abstract_quality": 0-10,     // 摘要是否真实概括内容(套话空话/与正文不符会扣分)
 "conclusion_echo": 0-10,      // 结论是否回应题目与摘要
 "ref_issues": ["问题描述"],    // 参考文献格式/年份/凑数等异常, 没有则[]
 "hard_flaws": ["确定矛盾:片段内证据"], // 片段内可见的数值/事实矛盾(须引用原文数字), 没有则[]
 "weak_notes": ["弱提示"],      // 缺依据类提示, 没有则[]
 "verdict": "pass|warn|fail",  // fail=有hard_flaws确定矛盾; warn=有疑点/弱提示; pass=无
 "summary": "一句话总评(30字内)"
}}"""


def build_prompt(mat):
    ch = "\n".join(f"· {h} | {fp}" for h, fp in mat["chapters"])
    return PROMPT_TMPL.format(
        title=mat["title"] or "(未提取到)",
        abstract=mat["abstract"] or "(未提取到)",
        chapters=ch or "(未提取到)",
        conclusion=mat["conclusion"] or "(未提取到)",
        refs="\n".join(mat["refs"]) or "(未提取到)",
    )


def parse_json(txt):
    txt = txt.strip()
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        try:
            fixed = re.sub(r",\s*([\]}])", r"\1", m.group(0))
            return json.loads(fixed)
        except Exception:
            return None


def _save(results, path):
    """逐篇原子落盘: 崩溃最多丢当前一篇, 不再每10篇批量写。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=0)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# 只有终态可视为"已完成"; retry/error 重跑时必须重试, 不得当done跳过
_TERMINAL = ("pass", "warn", "fail")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", required=True)
    ap.add_argument("--root", default="论文终版")
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--use-proxy", action="store_true", help="直接走 codex-proxy")
    args = ap.parse_args()
    files = json.load(open(args.list))
    if args.limit:
        files = files[:args.limit]
    results = {}
    if os.path.exists(args.out):
        try:
            results = json.load(open(args.out, encoding="utf-8"))
        except Exception:
            results = {}
    done = {k for k, v in results.items()
            if isinstance(v, dict) and v.get("verdict") in _TERMINAL}
    for i, rel in enumerate(files):
        if rel in done:
            continue
        path = os.path.join(args.root, rel)
        try:
            mat = extract_material(path)
        except Exception as e:
            results[rel] = {"verdict": "error", "evidence_scope": "片段",
                            "summary": f"素材提取失败:{type(e).__name__}"}
            _save(results, args.out)
            continue
        prompt = build_prompt(mat)
        # 路由: 默认百炼; 限额/失败3次 → proxy; proxy也连败3次 → sleep退避
        use_proxy = args.use_proxy or STATE["dashscope_fail"] >= 3
        ok, txt = call_llm(prompt, use_proxy=use_proxy)
        if not ok and not use_proxy:
            STATE["dashscope_fail"] += 1
            use_proxy = True
            ok, txt = call_llm(prompt, use_proxy=True)
        if not ok and use_proxy:
            STATE["proxy_fail"] += 1
            if STATE["proxy_fail"] >= 3:
                print(f"[{i}] 双通道连续失败暂停10s: {txt[:80]}", flush=True)
                time.sleep(10)
                STATE["proxy_fail"] = 0
            results[rel] = {"verdict": "retry", "evidence_scope": "片段",
                            "summary": txt[:120]}
            _save(results, args.out)
            continue
        if ok:
            STATE["proxy_fail"] = 0
        parsed = parse_json(txt)
        if not parsed:
            results[rel] = {"verdict": "retry", "evidence_scope": "片段",
                            "summary": "JSON解析失败", "raw": txt[:200]}
            _save(results, args.out)
            continue
        # evidence_scope显式标注: 本层只看片段, 任何结论不得冒充全文终审
        parsed["evidence_scope"] = "片段"
        parsed["fragments"] = {
            "chapters_seen": len(mat["chapters"]),
            "abstract_chars": len(mat["abstract"]),
            "refs_seen": len(mat["refs"]),
        }
        results[rel] = parsed
        _save(results, args.out)
        if (i + 1) % 10 == 0:
            n_ok = sum(1 for v in results.values() if v.get("verdict") in _TERMINAL)
            print(f"[{i+1}/{len(files)}] 已完成{n_ok}", flush=True)
    _save(results, args.out)
    from collections import Counter
    cnt = Counter(v.get("verdict", "?") for v in results.values())
    print("LLM语义审计(片段级证据):", dict(cnt), "->", args.out)


if __name__ == "__main__":
    main()
