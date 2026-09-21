# -*- coding: utf-8 -*-
"""st_v2.py — 审题程序增强版 (0908增强包)。
变更(相对 st.py):
1. 土木/机械/法学 从"直过"改为真实评判(规范性+专业性角度)
2. 默认 dry-run: 只输出判定结果, 不点任何网站按钮; --commit 才实际提交
3. 支持离线模式: --titles 题目清单json 批量判定(不登录网站)
4. 账密读环境变量 ST_USER/ST_PASS(不落盘明文; 仓库版无 st.py 回退)
"""
import os
import re
import sys
import json
import argparse
import logging
import requests
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("st_v2")

# LLM端点: 百炼(服务器.env)优先, 回退DeepSeek官方
_ENVF = "/root/project/workspace/thesis-reviser/.env"
if os.path.exists(_ENVF):
    for line in open(_ENVF):
        line = line.strip()
        if line.startswith("export "):
            line = line[7:]
        if "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))
DEEPSEEK_API_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1") + "/chat/completions"
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
_SRC = ""  # 服务器部署: 凭据走环境变量, 不读 st.py(不入库)
API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
LOGIN_URL = os.environ.get("ST_LOGIN_URL", "https://zk.wencaischool.net/#/login")
TARGET_URL = os.environ.get("ST_TARGET_URL", "https://zk.wencaischool.net/#/login")
USERNAME = os.environ.get("ST_USER", "")
PASSWORD = os.environ.get("ST_PASS", "")

# 专业分流(v3, 2026-09-20 用户指令): 服装/数媒/视觉从直过改为评判(须有具体研究对象)
DIRECT_PASS_MAJORS = []                      # v3: 无直过专业
STRICT_MAJORS = ["财务管理", "人力资源管理", "金融学"]  # 原有严格审(具体研究对象)
JUDGE_MAJORS = {"土木工程", "机械工程", "机械设计制造及其自动化", "法学",
                "服装与服饰设计", "数字媒体艺术", "视觉传达设计"}  # v3设计类入评判
ENV_MAJORS = {"环境设计"}  # 原有: 具体建筑/场所

MAJOR_PROMPTS = {
    "土木": """
该学生专业是【土木工程】, 从题目规范性和专业性角度评判:
- 规范性: 题目必须指向具体工程项目/建筑对象(如"XX办公楼""XX小区X号楼""XX桥梁""XX道路"),
  研究类型表述明确(结构设计/基础工程设计/施工组织设计/加固改造/沉降分析等), 不得泛泛而谈
- 专业性: 应体现明确的结构体系或工程类型(框架结构/剪力墙/钢结构/地基基础/道路桥梁等);
  "XX工程施工图设计"比"建筑研究""工程探讨"这类空泛表述合格
- 不合格示例: "浅谈建筑设计""土木工程技术研究""建筑结构分析"(无具体对象+无明确类型)
- 合格示例: "无锡某办公楼框架结构设计""XX小区3号楼桩基础工程设计"
""",
    "机械": """
该学生专业是【机械类】, 从题目规范性和专业性角度评判:
- 规范性: 题目必须指向具体机械装置/设备/系统(如"XX机床进给机构""小型玉米脱粒机""XX减速器"),
  研究类型明确(设计/分析/优化/故障诊断/仿真)
- 专业性: 应体现机械专业领域内涵(机构设计/传动系统/结构分析/制造工艺/机电控制);
  "机械设计研究""智能制造探讨"这类空泛表述不合格
- 合格示例: "小型物料搬运机械手结构设计""XX车床主轴箱设计""曲轴振动抛光机设计"
""",
    "设计": """
该学生专业是【设计类(服装与服饰设计/数字媒体艺术/视觉传达设计)】, 从题目规范性和专业性角度评判:
- 规范性: 题目必须指向具体设计对象/项目/主题(如"XX品牌视觉形象设计""'竹韵'主题系列女装设计"
  "XX城市IP形象设计""XX主题交互装置设计""XX文创产品系列设计"), 研究类型明确(设计/创作/优化/应用研究),
  不得只有领域泛论
- 专业性: 应体现本专业领域内涵(服装: 系列设计/结构工艺/面料再造; 数媒: 交互/影视/动画/装置;
  视觉: 品牌形象/导视/插画/包装), "服装设计研究""数字媒体艺术探讨""视觉传达浅析"这类无对象泛论不合格
- 不合格示例: "服装设计与传统文化融合研究""数字媒体艺术创作研究""视觉传达设计发展趋势探讨"
- 合格示例: "'江南织造'主题系列女装设计""社区养老App交互界面设计——以XX社区为例""故宫文创产品视觉系列设计"
""",
    "法学": """
该学生专业是【法学】, 从题目规范性和专业性角度评判:
- 规范性: 题目应为规范的法律论文题目形态, 以下三种之一:
  ① 案例分析型: "XX案中XX问题的法律分析" / "从XX案看XX制度"
  ② 规范分析型: "XX制度的完善/构成/适用研究"(须聚焦具体制度, 不得整部门法泛论)
  ③ 案例评释型: "XX案的请求权基础分析/鉴定式分析"
- 专业性: 聚焦具体法律问题(具体罪名/合同类型/权利义务/程序问题), 使用规范法学术语
- 不合格示例: "论合同法""刑法研究""浅谈法律与道德"(无具体问题+无专业聚焦)
- 合格示例: "于欢案中正当防卫限度的法律分析""情势变更制度在房屋买卖合同中的适用研究"
""",
}


def major_requirement(major):
    if major in STRICT_MAJORS:
        return "STRICT"  # 沿用st.py原经管严格审文本
    if major in ENV_MAJORS:
        return "ENV"
    if "土木" in major:
        return MAJOR_PROMPTS["土木"]
    if "机械" in major:
        return MAJOR_PROMPTS["机械"]
    if "法学" in major:
        return MAJOR_PROMPTS["法学"]
    if any(k in major for k in ("服装", "数字媒体", "视觉")):
        return MAJOR_PROMPTS["设计"]
    return None


STRICT_TEXT = """
该学生专业是【严格审核专业】, 必须包含具体研究对象(如xx公司的xx问题研究)。
只有泛泛理论讨论无具体案例/企业名称则不通过。"""

ENV_TEXT = """
该学生专业是【环境设计】, 题目必须包含具体建筑或场所(XX公园/XX小区/XX咖啡馆等)。
无明确具体场所则不通过。"""


def check_title(title, major):
    """DeepSeek判定。返回 (passed, reason)。"""
    req = major_requirement(major)
    if req is None:
        return True, "专业在直过名单, 未评判"
    if req == "STRICT":
        req = STRICT_TEXT
    elif req == "ENV":
        req = ENV_TEXT
    prompt = f"""你是严格的论文题目审核专家。只基于题目本身判断, 不得编造题目中不存在的信息。

题目: {title}
专业: {major}
{req}

判定规则: 规范性与专业性双达标→通过; 任一不达标→不通过。
【研究对象认定(统一规则)】研究对象的存在按以下四形态认定, 均视为具备具体研究对象:
  ① 具体名称前置: "无锡XX办公楼框架结构设计"
  ② 脱敏表述前置: "某高校/某企业/某公司/A公司/B集团…"开头或含于主标题
  ③ 具体名称做副标题: "…研究——以XX公司/XX公园为例"
  ④ 脱敏表述做副标题: "…研究——以某公司/某高校为例"
只判定是否存在可锚定的研究对象, 不得因脱敏或不具名而判不合格; 四形态任一满足即认定有对象。
返回JSON: {{"passed": true/false, "reason": "指明题目中的具体对象与类型(通过)或缺陷所在(不通过)", "维度": {{"规范性": "达标/不达标+一句说明", "专业性": "达标/不达标+一句说明"}}}}"""
    try:
        r = requests.post(DEEPSEEK_API_URL,
                          headers={"Authorization": f"Bearer {API_KEY}"},
                          json={"model": DEEPSEEK_MODEL,
                                "messages": [{"role": "system", "content": "你是严谨的论文题目审核专家, 只基于题目文本判断。"},
                                             {"role": "user", "content": prompt}],
                                "temperature": 0.01, "max_tokens": 600,
                                "response_format": {"type": "json_object"},
                                "enable_thinking": False},
                          timeout=60)
        if r.status_code == 400 and "enable_thinking" in r.text:
            body = {"model": DEEPSEEK_MODEL,
                    "messages": [{"role": "system", "content": "你是严谨的论文题目审核专家, 只基于题目文本判断。"},
                                 {"role": "user", "content": prompt}],
                    "temperature": 0.01, "max_tokens": 600,
                    "response_format": {"type": "json_object"}}
            r = requests.post(DEEPSEEK_API_URL, headers={"Authorization": f"Bearer {API_KEY}"}, json=body, timeout=60)
        j = json.loads(r.json()["choices"][0]["message"]["content"])
        return bool(j.get("passed")), j.get("reason", "")
    except Exception as e:
        return None, f"判定失败:{type(e).__name__}"


def offline_run(titles_file):
    items = json.load(open(titles_file, encoding="utf-8"))
    out = []
    for it in items:
        t, m = it["title"], it["major"]
        if m in DIRECT_PASS_MAJORS:
            out.append({**it, "passed": True, "reason": "直过名单", "mode": "direct"})
            continue
        p, r = check_title(t, m)
        out.append({**it, "passed": p, "reason": r, "mode": "judge"})
        log.info(f"[{'通过' if p else '不通过'}] {m} {t[:30]} | {r[:60]}")
    outf = titles_file.rsplit(".", 1)[0] + "_判定.json"
    json.dump(out, open(outf, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    ok = sum(1 for x in out if x["passed"])
    print(f"\n离线判定: {ok}/{len(out)} 通过 → {outf}")


def site_run(commit=False, defense="书面答辩"):
    """网站模式: 登录→筛选→逐条判定→(仅--commit时提交)。默认只读+输出判定。"""
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    import time
    opts = webdriver.ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    d = webdriver.Chrome(options=opts)
    results = []
    try:
        d.get(LOGIN_URL)
        time.sleep(3)
        d.find_element(By.CSS_SELECTOR, 'input[placeholder="请输入学号/证件号"]').send_keys(USERNAME)
        d.find_element(By.CSS_SELECTOR, 'input[placeholder="请输入密码"][type="password"]').send_keys(PASSWORD)
        cb = d.find_element(By.CSS_SELECTOR, 'input[name="type"][type="checkbox"]')
        if not cb.is_selected():
            cb.click()
        d.find_element(By.CSS_SELECTOR, "div.submitBtn").click()
        time.sleep(5)
        if "login" in d.current_url.lower():
            print("登录失败(账密失效?)——中止, 未做任何操作")
            return
        d.get(TARGET_URL)
        time.sleep(4)
        # 筛选: 未审核 + 指定答辩方式
        d.find_element(By.XPATH, '//label[text()="选题状态"]/following::div[contains(@class,"ant-select-selection")][1]').click()
        time.sleep(1)
        d.find_element(By.XPATH, '//li[text()="未审核"]').click()
        time.sleep(1)
        d.find_element(By.XPATH, '//label[text()="答辩方式"]/following::div[contains(@class,"ant-select-selection")][1]').click()
        time.sleep(1)
        li = d.find_elements(By.XPATH, f'//li[text()="{defense}"]')
        if li:
            li[0].click()
        time.sleep(1)
        d.find_element(By.XPATH, '//button/span[text()="查 询"]/parent::button').click()
        time.sleep(3)
        rows = d.find_elements(By.CSS_SELECTOR, "tr.ant-table-row")
        print(f"筛选[{defense}/未审核]: {len(rows)} 条")
        for i in range(min(len(rows), 50)):
            try:
                rows = d.find_elements(By.CSS_SELECTOR, "tr.ant-table-row")
                rows[i].find_elements(By.XPATH, './/a[text()="查看"]')[0].click()
                time.sleep(1.5)
                tcell = d.find_element(By.XPATH, '//div[@class="ant-modal-body"]//td[@class="ant-table-row-cell-break-word"][1]')
                title = tcell.text.strip()
                # 行内专业(第3-4列尝试)
                major = ""
                for td in rows[i].find_elements(By.TAG_NAME, "td"):
                    txt = td.text.strip()
                    if any(k in txt for k in ("工程", "管理", "设计", "法学", "财务", "金融", "机械")):
                        major = txt
                        break
                p, r = check_title(title, major or "未知")
                results.append({"title": title, "major": major, "passed": p, "reason": r})
                log.info(f"[{'通过' if p else '不通过'}] {major} {title[:32]} | {r[:50]}")
                # 关闭模态框
                for btn in d.find_elements(By.XPATH, '//div[@role="dialog"]//button[@aria-label="Close"] | //div[@class="ant-modal-close"]'):
                    try:
                        btn.click()
                        break
                    except Exception:
                        continue
                time.sleep(1)
                if commit:
                    print("!! commit模式未实现提交(默认dry-run保护)——仅输出判定")
                    break
            except Exception as e:
                log.warning(f"第{i}条处理异常: {type(e).__name__}")
                continue
    finally:
        d.quit()
        if results:
            outf = f"st_v2_判定_{datetime.now():%m%d_%H%M}.json"
            json.dump(results, open(outf, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            ok = sum(1 for x in results if x["passed"])
            print(f"\n网站判定({defense}): {ok}/{len(results)} 通过 → {outf}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--titles", help="离线模式: 题目清单json [{title,major}]")
    ap.add_argument("--commit", action="store_true", help="实际提交到网站(默认dry-run只判定)")
    ap.add_argument("--defense", default="书面答辩", help="答辩方式筛选(默认书面答辩)")
    a = ap.parse_args()
    if a.titles:
        offline_run(a.titles)
    else:
        site_run(commit=a.commit, defense=a.defense)


if __name__ == "__main__":
    main()
