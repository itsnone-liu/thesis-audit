# -*- coding: utf-8 -*-
"""lw_gx_tm.py — 机械/土木类工科论文评审器 (0908增强包)。
对齐 lwjg.py 的 review_thesis(file_path) 接口契约, 可直接接入 batch_v2.py。
特点: ①工科框架严格要求 ②图表数据支撑为硬性必达项 ③审计原则细化(audit_principles)
     ④确定性预检证据注入prompt(防LLM幻觉)。
"""
import os
import re
import json
import time
import logging
import requests
from docx import Document
from audit_principles import precheck, evidence_block, PRINCIPLES_TEXT

AUDIT_EXCEPTIONS = ("【研究对象认定规则】研究对象四形态均有效: ①具体名称前置 ②脱敏表述前置"
                    "('某高校''某企业''某公司''A公司''B集团'等) ③具体名称做副标题'——以XX为例'"
                    " ④脱敏做副标题'——以某XX为例'。只判定是否存在可锚定的研究对象,"
                    "不得因脱敏/不具名/位置形态判不合格; 此规则优先于其他题目规则。")

# LLM端点: 优先百炼(服务器.env配置), 回退DeepSeek官方
_ENVF = "/root/project/workspace/thesis-reviser/.env"
if os.path.exists(_ENVF):
    for line in open(_ENVF):
        line = line.strip()
        if line.startswith("export "):
            line = line[7:]
        if "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))
API_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1") + "/chat/completions"
API_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")


class ThesisEngReviewer:
    """机械/土木工科评审器。etype: '机械' | '土木'"""

    def __init__(self, api_key: str = None, etype: str = "机械"):
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        self.etype = etype
        self.logger = logging.getLogger(f"lw_{etype}")
        if not self.logger.handlers:
            logging.basicConfig(level=logging.INFO,
                                format="%(asctime)s - %(levelname)s - %(message)s")

    # ---------- 提取 ----------
    def extract(self, path):
        doc = Document(path)
        paras = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
        # 题目: 封面"论文题目"行或首个长标题
        title = ""
        for i, t in enumerate(paras[:40]):
            m = re.match(r"论文题目[:：]\s*(.+)", t)
            if m:
                title = m.group(1).strip()
                break
        if not title:
            title = next((t for t in paras if 12 <= len(t) <= 45 and "摘" not in t), "")
        # 章标题
        heads = [t for t in paras if re.match(r"^第[一二三四五六七八\d]+章|^\d(\.\d){0,2}\s", t)]
        full = "\n".join(paras)
        # 截断送审(工科重点: 摘要+章标题+各章首段+结论+图表注)
        material = paras[:20] if len(paras) > 400 else paras
        return title, heads, "\n".join(material)[:14000], full

    # ---------- LLM(百炼兼容端点, enable_thinking:false防推理模型空正文) ----------

    def call(self, messages, max_tokens=4000):
        try:
            if messages and "研究对象" in str(messages[-1].get("content", "")):
                messages[-1]["content"] = AUDIT_EXCEPTIONS + "\n" + str(messages[-1]["content"])
        except Exception:
            pass
        body = {"model": API_MODEL, "messages": messages,
                "temperature": 0.01, "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
                "enable_thinking": False}
        for attempt in range(3):
            try:
                r = requests.post(API_URL,
                                  headers={"Authorization": f"Bearer {self.api_key}",
                                           "Content-Type": "application/json"},
                                  json=body, timeout=120)
                if r.status_code == 200:
                    j = r.json()
                    content = (j.get("choices") or [{}])[0].get("message", {}).get("content", "")
                    if not content:  # 空正文→升高max_tokens重试(推理模型静默故障)
                        body["max_tokens"] = min(int(body["max_tokens"] * 1.8), 16000)
                        self.logger.warning("空正文, max_tokens→%d 重试", body["max_tokens"])
                        continue
                    return j
                if r.status_code == 400 and "enable_thinking" in r.text:
                    body.pop("enable_thinking", None)
                    continue
                self.logger.warning(f"API {r.status_code}, 重试{attempt+1}")
            except Exception as e:
                self.logger.warning(f"API异常{type(e).__name__}, 重试{attempt+1}")
            time.sleep(3)
        return None

    # ---------- 核心: 评审 ----------
    def review_with_deepseek(self, text, title, heads, pc):
        frame = {
            "机械": """1. 绪论(研究背景/意义/国内外现状)
2. 总体方案设计或理论分析(机构原理/系统方案/工艺方案对比与选择)
3. 具体设计计算/分析(结构设计、参数计算、强度校核、运动/力学分析)
4. 仿真或实验验证(可选但强烈期望: 三维建模/有限元/性能测试)
5. 结论
必须有设计计算过程(公式+参数代入+结果), 不能只有定性描述。""",
            "土木": """1. 绪论(工程背景/设计依据)
2. 设计资料/工程概况(荷载取值、地质条件、材料参数)
3. 结构方案选型与布置(结构体系选择、构件布置)
4. 结构计算与分析(荷载组合、内力计算、配筋计算或验算)
5. 构造措施与施工要求(或基础/节点设计)
6. 结论
必须有计算书性质的段落(荷载→内力→截面/配筋), 不能只有描述性文字。"""
        }[self.etype]

        prompt = f"""你是一位严格的工科本科毕业论文评审专家({self.etype}类)。请对以下论文进行评审。

## 论文信息
- 题目: {title}
- 章节结构: {heads[:30]}

## 确定性预检证据(程序实测, 优先于你的目测, 不得否认)
{evidence_block(pc)}

## 评审要求
### 一、必达项(任何一项不合格即总体不合格)
1. 题目: {self.etype}类题目必须含具体工程对象/装置系统(如"XX办公楼框架结构设计""XX机构的设计与分析"),
   表述规范(研究类型明确: 设计/分析/优化/诊断), 不得泛泛而谈
2. 字数: 10000字以上(预检实测为准)
3. 结构: 必须包含工科框架:
{frame}
4. 图表数据支撑【{self.etype}类硬性要求】: 图片≥5张(结构图/原理图/模型图/力学简图等, 预检实测为准),
   表格≥3个(参数表/荷载表/结果对比表等); 计算与结论必须有数据支撑, 纯文字论述不合格
5. 语言: 专业通顺, 术语正确({self.etype}专业术语使用得当)

### 二、审计细化条款(逐项核查, 写入对应维度评语)
{PRINCIPLES_TEXT}

## 论文文本(节选)
{text}


## 评审报告撰写规范(最高优先级, 适用于所有评语与"评审报告"字段)
1. 评语以自然语言撰写, 像一位毕业论文指导教师在评阅: 禁止"实测""检测到""程序判定""证据""预检""指标显示"等程序化词汇;
   数据表述用自然形式(如"全文约1.4万字, 插图14幅、表格3张")。
2. JSON新增顶层字段 "评审报告"(字符串): 一篇完整评语, 严格按三部分组织, 每部分以标记行开头:
【总体评价】2-4句: 论文整体质量与结论。若合格, 写明建议成绩(如"综合评定为合格, 建议成绩82分");
  若不合格, 只作定性结论, 不得出现任何分数、分值、得分。
3. 【具体评价】(在"评审报告"内): 按题目、结构、内容与论证、语言与规范等维度逐段展开,
   所有具体问题在此详细指出(引到章节、数值、图表编号等原文位置), 用连贯段落, 不用表格罗列。
4. 【修改意见】(在"评审报告"内): 逐条列出, 具体可执行; 不合格论文的修改意见要覆盖全部主要缺陷。
5. 其余分项字段照常输出(供内部判定), 但"评审报告"是最终呈现文本, 必须自足完整。

## 输出JSON格式
{{
  "论文题目": "{title}",
  "是否合格": true/false,
  "评审报告": "【总体评价】...【具体评价】...【修改意见】...",
  "总分": 0-100,
  "分项评分": {{
    "题目": {{"得分": 0-15, "评语": "...", "问题": []}},
    "结构框架": {{"得分": 0-25, "评语": "...", "缺失章节": [], "是否有重复论述": false, "问题": []}},
    "图表数据支撑": {{"得分": 0-25, "评语": "...", "图表统计": {{"表格数量": 0, "图片数量": 0, "质量评价": "..."}}, "问题": []}},
    "计算分析深度": {{"得分": 0-15, "评语": "设计计算/验算过程是否完整充分", "问题": []}},
    "语言与一致性": {{"得分": 0-10, "评语": "含审计细化条款A-F核查结论", "问题": []}},
    "格式": {{"得分": 0-10, "评语": "...", "问题": []}}
  }},
  "必达项检查": {{
    "题目合格": true/false, "字数合格": true/false, "结构合格": true/false,
    "图表数据合格": true/false, "语言合格": true/false
  }},
  "审计条款核查": {{
    "A数值一致性": "通过/发现问题+说明", "B图表引用一致性": "通过/发现问题+说明",
    "C数量词一致性": "通过/未涉及", "D题目内容匹配": "通过/发现问题+说明",
    "E摘要结论呼应": "通过/发现问题+说明", "F称谓统一": "通过/发现问题+说明"
  }},
  "总体评语": "...",
  "修改建议": ["建议1", "建议2", "建议3"]
}}"""
        messages = [
            {"role": "system", "content": f"你是严格的{self.etype}类本科毕业论文评审专家。必达项任一不合格即不合格。"},
            {"role": "user", "content": prompt},
        ]
        resp = self.call(messages)
        if resp and "choices" in resp:
            try:
                return json.loads(resp["choices"][0]["message"]["content"])
            except Exception as e:
                self.logger.error(f"JSON解析失败: {e}")
                return {"是否合格": None, "总分": 0, "总体评语": f"解析失败:{e}", "解析失败": True}
        return {"是否合格": None, "总分": 0, "总体评语": "API调用失败", "API失败": True}

    def review_thesis(self, file_path: str) -> dict:
        self.logger.info(f"开始审核: {file_path}")
        title, heads, text, full = self.extract(file_path)
        pc = precheck(file_path)
        result = self.review_with_deepseek(text, title, heads, pc)
        # 程序侧硬性规则(不依赖LLM自觉):
        # ① 必达项任一False → 总体不合格  ② 确定性预检硬线: 图<5或表<3→图表必达项False
        must = result.get("必达项检查") or {}
        if must.get("图表数据合格") is None or must.get("图表数据合格") is True:
            if pc["image_count"] < 5 or pc["table_count"] < 3:
                must["图表数据合格"] = False
                result["必达项检查"] = must
                result["是否合格"] = False
                result.setdefault("总体评语", "")
                result["总体评语"] = f"[程序判定: 图{pc['image_count']}张/表{pc['table_count']}个 未达工科硬线(图≥5,表≥3)] " + str(result.get("总体评语", ""))
        if result.get("是否合格") and any(v is False for v in must.values()):
            result["是否合格"] = False
            result["总体评语"] = "[程序判定: 必达项未全过] " + str(result.get("总体评语", ""))
        return {"result": result, "thesis_info": {"title": title, "stats": pc}, "file": file_path}
