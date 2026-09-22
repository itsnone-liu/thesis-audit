# -*- coding: utf-8 -*-
"""lw_stage_audit.py — 网站论文初稿审核程序 v3 (2026-09-22, 弹窗实勘适配+成绩表)。

业务口径(用户定):
  ① 只筛 [初稿已提交且未审核] 的学生
  ② 只审 [现场答辩] 的论文(书面答辩不审)——页面筛选器主动设为现场答辩
  ③ --commit 真实提交: 审核结果(通过/不通过) + 意见附件(剥分txt) → 保存 → 回读验证
  ④ LLM评语合格→提取成绩, 落本地 [论文成绩表.csv]; 上传意见剥除成绩只留合格/不合格

用法:
  python3 lw_stage_audit.py                    # dry-run: 筛选+下载+审核+意见, 零写操作
  python3 lw_stage_audit.py --commit           # 真实提交(结果+意见附件, 逐条回读验证)
  python3 lw_stage_audit.py --only 李明辉 --commit   # 定点真实提交某学生
  python3 lw_stage_audit.py --pages 2 --limit 10
  python3 lw_stage_audit.py --any --limit 1    # 调试重放(不限初稿/待审, 不提交)
"""
import os
import re
import sys
import json
import time
import socket
import argparse
import logging
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# 全局socket兜底超时(2026-09-22 老刘指示: 下载大文件不卡死, connect阶段20s必断)
# 注意: setdefaulttimeout 对未显式传timeout的socket生效, requests/urllib显式传参优先。
socket.setdefaulttimeout(30)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("stage_audit")

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "../批量"))
_SRC = ""  # 凭据走环境变量 ST_USER/ST_PASS, st.py 不入库
LOGIN_URL = os.environ.get("ST_LOGIN_URL", "https://zk.wencaischool.net/#/login")
PAGE_URL = "https://zk.wencaischool.net/#/thesisAssignStu"

_ENVFS = ["/root/hworkspace/thesis-audit/.env",
          "/root/project/workspace/thesis-reviser/.env"]
for _ENVF in _ENVFS:
    if os.path.exists(_ENVF):
        for line in open(_ENVF):
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            if "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))

_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1")
_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash-0731")
USERNAME = os.environ.get("ST_USER", "")
PASSWORD = os.environ.get("ST_PASS", "")

TARGET_STAGE = "初稿"          # 业务口径①: 只审初稿
DEFENSE_MODE = "现场答辩"       # 业务口径②: 只审现场答辩
STATUS_WORDS = ("审核通过", "审核不通过", "未审核", "待审核", "已退回", "退回修改")
# 审核不通过原因(必填文本框, 2026-09-22 audit.py实勘: input[placeholder="请输入"]; 与生产程序一致填"见附件", 详细原因在意见附件)
REJECT_REASON = "见附件"
OUT_DIR = os.path.join(_HERE, "stage_audit_out")
DOWN_DIR = os.path.join(OUT_DIR, "downloads")


# ---------------------------------------------------------------- 浏览器
def make_driver():
    opts = webdriver.ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--blink-settings=imagesEnabled=false")   # 禁图加速(弹窗纯文本)
    opts.page_load_strategy = "eager"                            # DOM就绪即续
    # 下载目录固定到 DOWN_DIR(详情弹窗"下载"链接为JS触发, 需prefs承接)
    os.makedirs(DOWN_DIR, exist_ok=True)
    prefs = {"download.default_directory": DOWN_DIR,
             "download.prompt_for_download": False,
             "download.directory_upgrade": True,
             "safebrowsing.enabled": True}
    opts.add_experimental_option("prefs", prefs)
    d = webdriver.Chrome(options=opts)
    # wire调用兜底超时(2026-09-22: headless偶发挂死, 无超时会无限阻塞)
    d.set_script_timeout(25)
    d.set_page_load_timeout(40)
    # find_elements等wf命令同样可能挂死: 给HTTP wire通道设30s超时(2026-09-22杜明顺卡死在find_elements)
    try:
        d.command_executor.set_timeout(30)
    except Exception:
        pass
    return d


def wait_table(d, timeout=25, retries=2):
    """表格就绪等待; 超时refresh重试(偶发加载卡)。"""
    for i in range(retries):
        try:
            WebDriverWait(d, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "tbody tr td")))
            return True
        except Exception:
            if i < retries - 1:
                log.warning(f"表格等待超时, refresh重试({i+1})")
                d.refresh()
                time.sleep(6)
    log.warning("表格仍未就绪, 继续尝试")
    return False


def wait_clickable(d, locator, timeout=10):
    """等待元素可见(翻页等), 超时抛异常。"""
    for _ in range(timeout):
        try:
            el = d.find_element(*locator)
            if el.is_displayed():
                return el
        except Exception:
            pass
        time.sleep(1)
    raise Exception(f"元素超时: {locator}")


def login(d):
    d.get(LOGIN_URL)
    WebDriverWait(d, 20).until(EC.presence_of_element_located(
        (By.CSS_SELECTOR, 'input[placeholder="请输入学号/证件号"]')))
    # 等 loadMask 消失, 防遮挡点击(实测偶发被拦截)
    for _ in range(10):
        try:
            if not d.find_elements(By.CSS_SELECTOR, "div.loadMask"):
                break
        except Exception:
            break
        time.sleep(1)
    d.find_element(By.CSS_SELECTOR, 'input[placeholder="请输入学号/证件号"]').send_keys(USERNAME)
    d.find_element(By.CSS_SELECTOR, 'input[placeholder="请输入密码"][type="password"]').send_keys(PASSWORD)
    cb = d.find_element(By.CSS_SELECTOR, 'input[name="type"][type="checkbox"]')
    if not cb.is_selected():
        js_click(d, cb)
    d.find_element(By.CSS_SELECTOR, "div.submitBtn").click()
    for _ in range(10):
        time.sleep(2)
        if "login" not in d.current_url.lower():
            log.info("登录成功")
            return
    raise RuntimeError("登录失败(账密失效?)")


def js_click(d, el):
    try:
        el.click()
    except Exception:
        d.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        d.execute_script("arguments[0].click();", el)


def close_all_modals(d, settle=2.0):
    for _ in range(4):
        try:
            closes = d.find_elements(By.CSS_SELECTOR, ".ant-modal-close")
            if not closes:
                return
            for x in reversed(closes):
                try:
                    x.click()
                    time.sleep(0.5)
                except Exception:
                    js_click(d, x)
        except Exception:
            return
    if settle:
        time.sleep(settle)   # 关弹窗后表格常重渲染, 等稳定


def last_modal(d):
    ms = [m for m in d.find_elements(By.CSS_SELECTOR, ".ant-modal") if m.is_displayed()]
    return ms[-1] if ms else None


# ---------------------------------------------------------------- 页面筛选(答辩方式/阶段)
# 实测筛选器索引(稳定): 0批次 1主考院校 2年级 3季度 4类型 5专业 6老师 7班级
#                      8选题情况 9选题状态 10答辩方式 11开题 12初稿 13复稿 14终稿 15查重 16进度
_FILTER_INDEX = {"答辩方式": 10, "初稿": 12}

def _locate_select(d, label=None):
    """定位筛选下拉(每次fresh查询防stale): 优先label(nearest form-item), 其次实测索引。"""
    if label:
        for sel in d.find_elements(By.CSS_SELECTOR, "div.ant-select"):
            try:
                lab = " ".join(x.text.strip() for x in
                    sel.find_elements(By.XPATH,
                        "ancestor::div[contains(@class,'ant-form-item')][1]//label")
                    if x.text.strip()).rstrip(":* ")
                if lab == label:
                    return sel
            except Exception:
                continue
    idx = _FILTER_INDEX.get(label or "", -1)
    if idx >= 0:
        sels = d.find_elements(By.CSS_SELECTOR, "div.ant-select")
        if idx < len(sels):
            return sels[idx]
    return None


def _select_by_label(d, label, value):
    """在label对应的ant-select下拉(如 答辩方式/初稿)中选value。
    只操作目标下拉, 绝不遍历展开其他筛选器(遍历+body.click关闭有动画干扰误选,
    实测把"年级"误改成2020)。选择后fresh读回确认, 失败重试一次。"""
    target = _locate_select(d, label)
    if target is None:
        log.warning(f"筛选器[{label}]未找到")
        return False
    for attempt in (1, 2):
        try:
            js_click(d, target)
        except Exception:
            target = _locate_select(d, label)
            if target is None:
                return False
            js_click(d, target)
        time.sleep(1.0)
        picked = None
        try:
            for o in d.find_elements(By.CSS_SELECTOR, "li.ant-select-dropdown-menu-item"):
                if o.text.strip() == value and o.is_displayed():
                    picked = o
                    break
        except Exception:
            pass
        if picked is None:
            log.warning(f"筛选器[{label}]展开后未见选项[{value}]")
            return False
        try:
            js_click(d, picked)
        except Exception:
            return False
        time.sleep(0.6)
        # fresh读回确认
        cur = _locate_select(d, label)
        if cur is not None and value in (cur.text or ""):
            log.info(f"已设筛选: {label}={value}")
            return True
    log.warning(f"设置筛选[{label}={value}]两次均未确认成功")
    return False


def set_defense_filter(d, mode="现场答辩"):
    """页面筛选器: 答辩方式下拉设为 mode。成功返回True。"""
    return _select_by_label(d, "答辩方式", mode)


def set_stage_filter(d, label="初稿", value="未审核"):
    """页面筛选器: 阶段下拉(初稿/复稿/终稿/开题)设为 value(如未审核)。成功返回True。"""
    return _select_by_label(d, label, value)


def click_query(d):
    """点击[查询]按钮, 触发按当前筛选条件刷新表格。"""
    for b in d.find_elements(By.CSS_SELECTOR, "button"):
        if (b.text or "").replace(" ", "") == "查询":
            js_click(d, b)
            return True
    return False


def parse_major(row_text):
    """从学生行文本解析专业(列: 姓名 准考证号 年级 招生季度 专业 ...), 网页筛选可见, 优先于封面识别。"""
    parts = row_text.split("\n")[0].split()
    for i, w in enumerate(parts):
        if re.match(r"^(春季|秋季|春季/秋季)$", w) and i + 1 < len(parts):
            return parts[i + 1]
    # 兜底: 已知专业词匹配
    for w in parts:
        if any(k in w for k in ("工程", "管理", "设计", "媒体", "服装", "视觉", "法学", "金融", "会计", "教育", "旅游")):
            return w
    return ""


def student_rows(d):
    from selenium.common.exceptions import StaleElementReferenceException
    rows = []
    while True:
        try:
            for tr in d.find_elements(By.CSS_SELECTOR, "tbody tr"):
                t = tr.text.strip()
                if t and "暂无" not in t:
                    rows.append((tr, t))
            return rows
        except StaleElementReferenceException:
            rows = []  # 页面正在重渲染, 重新抓取
            time.sleep(0.8)


# ---------------------------------------------------------------- 解析
def parse_stages(modal_text):
    """逐行解析阶段行: '查看并审核初稿' 行后 1-3 行内找状态词。"""
    lines = [x.strip() for x in modal_text.split("\n")]
    stages = []
    for idx, ln in enumerate(lines):
        for st in ("开题", "初稿", "复稿", "终稿"):
            if ln == f"查看并审核{st}报告" or ln.startswith(f"查看并审核{st}"):
                status = ""
                for j in range(idx + 1, min(idx + 4, len(lines))):
                    if any(w in lines[j] for w in STATUS_WORDS):
                        status = lines[j].strip()
                        break
                stages.append({"stage": st, "status": status,
                               "pending": status not in ("审核通过", "审核不通过")})
    return stages


# ---------------------------------------------------------------- 审核(初稿)
_SCORE_SENT = re.compile(r"[，,。;；]?(综合)?(评定|评分|得分|成绩)[为]?\s*[0-9]{2,3}\s*分?")
_SCORE_RESIDUE = re.compile(r"\d{2,3}\s*分")


def strip_score(text):
    """上传意见剥分: 去掉'建议成绩82分'等分数表述, 只保留合格/不合格定性。"""
    if not text:
        return text
    text = _SCORE_SENT.sub("", text)
    text = _SCORE_RESIDUE.sub("", text)
    # 剥分后残留的孤立"建议XXX"片段(如"建议。"/"综合评定为合格，建议")
    text = re.sub(r"[，,。;；]?\s*建议(?=[，,。;；]|$)", "", text)
    text = re.sub(r"[，,。;；]\s*综合评定[为是]?\s*合格", "。综合评定为合格", text)
    text = re.sub(r"\s{2,}", " ", text).strip("，,。;； ")
    return text


def extract_score(passed, out=None, report="", opinion=""):
    """合格→提取成绩(int/None); 不合格→None。优先级: 评审器总分>报告>意见。"""
    if not passed:
        return None
    if out:
        try:
            v = int(out.get("result", {}).get("总分"))
            if v > 0:
                return v
        except Exception:
            pass
    for txt in (report, opinion):
        for m in _SCORE_SENT.finditer(txt or ""):
            for g in m.groups():
                if g and g.isdigit():
                    return int(g)
        m = re.search(r"(\d{2,3})\s*分", txt or "")
        if m:
            return int(m.group(1))
    return None


def review_draft(title, content_text, docx_path=None, web_major=""):
    """初稿审核: 有docx走评审器(新报告格式), 否则弹窗文本走LLM。
    返回(passed, opinion, report, score)。"""
    out = None
    if docx_path:
        try:
            passed, opinion, report, out = review_docx(docx_path, title, web_major=web_major)
            return passed, opinion, report, extract_score(passed, out=out, report=report, opinion=opinion)
        except Exception as e:
            log.warning(f"docx评审失败({type(e).__name__}), 回退LLM文本审: {str(e)[:60]}")
    passed, comment = llm_review_text(title, content_text)
    return passed, comment, None, extract_score(passed, opinion=comment)


def review_docx(path, title_hint="", web_major=""):
    """下载的初稿docx → 选评审器(专业以网页列表为准, 封面仅兜底) → (通过, 意见, 报告, out)。"""
    major = web_major
    if not major:
        from docx import Document
        doc = Document(path)
        for p in doc.paragraphs[:60]:
            m = re.match(r"专\s*业[:：]\s*(\S+)", p.text.strip())
            if m:
                major = m.group(1)
                break
    if any(k in major for k in ("机械",)):
        from lw_gx_tm import ThesisEngReviewer
        rv = ThesisEngReviewer(etype="机械")
    elif any(k in major for k in ("土木",)):
        from lw_gx_tm import ThesisEngReviewer
        rv = ThesisEngReviewer(etype="土木")
    elif any(k in major for k in ("财务管理", "人力资源管理", "金融", "会计", "工商", "旅游")):
        from lwjg import ThesisDeepSeekReviewer
        rv = ThesisDeepSeekReviewer()
    elif any(k in major for k in ("法学",)):
        from lwfx import ThesisLawReviewer
        rv = ThesisLawReviewer()
    else:
        from lwsj import ThesisDesignReviewer
        rv = ThesisDesignReviewer()
    out = rv.review_thesis(path)
    r = out["result"]
    import report_render as RR
    report = RR.render(out)
    passed = bool(r.get("是否合格"))
    # 意见=完整报告正文(总体评价+具体评价+修改意见全部保留, 给网站附件存档)
    # 2026-09-22 修复: 原只取【总体评价】一段且截断450字, 导致上传意见缺失具体评价/修改意见
    m = re.search(r"(【总体评价】.*)$", report, re.S)
    opinion = m.group(1).strip() if m else (report or str(r.get("总体评语", "")))
    return passed, opinion, report, out


def llm_review_text(title, content_text):
    """弹窗文本LLM审(初稿无docx时的兜底)。"""
    import requests
    url = _BASE_URL + "/chat/completions"
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    model = _MODEL
    prompt = f"""你是毕业论文初稿审核专家。基于以下材料审查, 只依据给定材料, 不得编造。

论文题目: {title}
初稿内容(节选):
{content_text[:3500]}

审查: ①题目可锚定研究对象(具体名称/脱敏表述前置或副标题"——以XX为例"四形态均有效) ②结构是否具备论文基本框架 ③内容与题目对应性 ④语言规范性。本科水平衡量, 不苛求深度。

只输出JSON: {{"通过": true/false, "意见": "80-150字自然语言审核意见, 不出现分数"}}"""
    body = {"model": model, "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1, "max_tokens": 400, "enable_thinking": False}
    for attempt in range(3):
        try:
            r = requests.post(url, headers={"Authorization": f"Bearer {key}",
                                            "Content-Type": "application/json"},
                              json=body, timeout=90)
            if r.status_code == 200:
                txt = r.json()["choices"][0]["message"]["content"] or ""
                m = re.search(r"\{.*\}", txt, re.S)
                if m:
                    j = json.loads(m.group(0))
                    return bool(j.get("通过")), str(j.get("意见", ""))[:450]
        except Exception as e:
            log.warning(f"LLM第{attempt+1}次失败: {type(e).__name__}")
        time.sleep(2 * (attempt + 1))
    return None, "API失败, 未提交"


# ---------------------------------------------------------------- 提交(--commit)
def _reason_input(modal):
    """定位'审核不通过原因'输入框: 优先 label→form-item→input, 兜底 placeholder=请输入。

    2026-09-22实勘(audit.py参考): 选[不通过]后该必填控件是 text input, 不是checkbox/下拉;
    不填会导致保存被校验拦截, 学生停留在'未审核'列表(李轩宇bug根因)。
    """
    try:
        for it in modal.find_elements(By.CSS_SELECTOR, ".ant-form-item"):
            labs = " ".join(x.text.strip() for x in it.find_elements(By.CSS_SELECTOR, "label"))
            if "审核不通过原因" in labs:
                for inp in it.find_elements(By.CSS_SELECTOR, "input"):
                    return inp
    except Exception:
        pass
    try:
        for inp in modal.find_elements(By.CSS_SELECTOR, 'input[placeholder="请输入"]'):
            return inp
    except Exception:
        pass
    return None


def submit_audit(d, modal, name, passed, opinion):
    """真实审核弹窗(2026-09-22实勘): 审核结果下拉(通过/不通过) + 意见txt附件 + [保存]。

    意见写入 uploads/{name}_意见.txt 作为审核附件上传; 返回(ok, msg)。"""
    # 1. 审核结果下拉
    sels = modal.find_elements(By.CSS_SELECTOR, "div.ant-select")
    if not sels:
        return False, "未找到审核结果下拉"
    try:
        js_click(d, sels[0])
    except Exception:
        d.execute_script("arguments[0].click();", sels[0])
    time.sleep(1.2)
    target = "通过" if passed else "不通过"
    chosen = False
    for o in d.find_elements(By.CSS_SELECTOR, "li.ant-select-dropdown-menu-item"):
        if (o.text or "").strip() == target and o.is_displayed():
            js_click(d, o)
            chosen = True
            break
    if not chosen:
        return False, f"审核结果下拉未找到[{target}]"
    time.sleep(0.8)
    # 1.5 审核不通过 → 必填"审核不通过原因"输入框(实勘: input[placeholder="请输入"], 2026-09-22)
    if not passed:
        r = _reason_input(modal)
        if r is None:
            return False, "未找到[审核不通过原因]输入框"
        try:
            r.clear()
            r.send_keys(REJECT_REASON)
            log.info(f"[{name}] 已填不通过原因: {REJECT_REASON}")
            time.sleep(0.5)
        except Exception as e:
            return False, f"填写不通过原因失败: {type(e).__name__}: {str(e)[:50]}"
    # 2. 意见写入txt → 上传为审核附件
    up_dir = os.path.join(OUT_DIR, "uploads")
    os.makedirs(up_dir, exist_ok=True)
    fpath = os.path.join(up_dir, f"{name}_意见.txt")
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(opinion or "（无意见）")
    fins = modal.find_elements(By.CSS_SELECTOR, "input[type=file]")
    if not fins:
        return False, "未找到审核附件上传框"
    fins[0].send_keys(fpath)
    # 轮询等待附件上传完成(弹窗出现"下载"标志, 实测需~6s; 2026-09-22孙涛因只等2s导致附件丢失)
    up_ok = False
    for _ in range(10):
        time.sleep(1.5)
        if "下载" in (modal.text or ""):
            up_ok = True
            break
    if not up_ok:
        log.warning(f"[{name}] 附件上传超时(15s未见下载标志), 仍尝试保存")
    # 3. [保存]
    btn = None
    for b in modal.find_elements(By.CSS_SELECTOR, "button"):
        if (b.text or "").replace(" ", "") == "保存":
            btn = b
            break
    if not btn:
        return False, "未找到[保存]按钮"
    js_click(d, btn)
    time.sleep(2.5)
    # 4. 可能的二次确认与成功提示
    for _ in range(2):
        try:
            okb = d.find_element(By.CSS_SELECTOR, ".ant-modal-confirm-btns .ant-btn-primary")
            js_click(d, okb)
            time.sleep(1.5)
        except Exception:
            break
    for _ in range(4):
        try:
            msgs = d.find_elements(By.CSS_SELECTOR, ".ant-message-notice, .ant-message")
            for m in msgs:
                t = m.text.strip()
                if t:
                    return True, f"弹窗反馈: {t[:60]}"
        except Exception:
            pass
        # 审核弹窗关闭=提交生效常见表现(背景详情弹窗仍在)
        try:
            dms = [x for x in d.find_elements(By.CSS_SELECTOR, ".ant-modal")
                   if x.is_displayed() and ("审核结果" in (x.text or ""))]
            if not dms:
                return True, "审核弹窗已关闭(提交生效)"
        except Exception:
            pass
        time.sleep(1)
    return True, "已点保存(未见明确反馈, 以回读为准)"


def verify_saved(d, name, stage, opinion_head):
    """回读验证: 重开该生阶段详情, 确认审核状态已变+意见已存。

    注意: 验证会临时切 初稿=所有 找已移出'未审核'的学生 → finally 必须恢复
    初稿=未审核 并重查, 否则批量模式下后续所有学生定位失败(2026-09-22 实测踩坑)。
    """
    filter_touched = False
    try:
        close_all_modals(d)
        time.sleep(1)
        views = d.find_elements(
            By.XPATH, f'//tbody/tr[.//td[contains(text(),"{name}")]]//a[text()="查看"]')
        if not views:
            # 提交成功后该生从"未审核"筛选消失是常见现象 → 清筛选(阶段=所有)重查一次
            try:
                _select_by_label(d, "初稿", "所有")
                click_query(d)
                filter_touched = True
                time.sleep(3)
                wait_table(d)
                views = d.find_elements(
                    By.XPATH, f'//tbody/tr[.//td[contains(text(),"{name}")]]//a[text()="查看"]')
            except Exception:
                pass
        if not views:
            return True, "回读: 无该生'查看'(提交后已移出列表, 请以详情为准)"
        js_click(d, views[0])
        modal = None
        for _ in range(10):
            time.sleep(1.5)
            modal = last_modal(d)
            if modal:
                break
        if not modal:
            return False, "回读: 详情弹窗未开"
        txt = modal.text
        newst = ""
        trs = modal.find_elements(By.CSS_SELECTOR, "table tbody tr")
        if trs:
            tcells = [c.text.strip() for c in trs[-1].find_elements(By.CSS_SELECTOR, "td")]
            if len(tcells) >= 3 and tcells[2] in ("审核通过", "审核不通过"):
                newst = tcells[2]
        if not newst:
            stages = parse_stages(txt)
            target = [s for s in stages if s["stage"] == stage]
            newst = target[0]["status"] if target else ""
        if newst in ("审核通过", "审核不通过"):
            saved_op = opinion_head[:10] in txt
            return True, f"回读验证: {newst}" + ("+意见已存" if saved_op else "(意见未见, 请人工核对)")
        return False, f"回读: 状态仍为[{newst or '空'}], 提交可能未生效"
    except Exception as e:
        return False, f"回读异常: {type(e).__name__}: {str(e)[:60]}"
    finally:
        # 无论验证结果如何, 恢复页面筛选, 避免破坏后续批量
        if filter_touched:
            try:
                _select_by_label(d, "初稿", "未审核")
                click_query(d)
                time.sleep(2)
                wait_table(d, timeout=15)
                log.info("回读完成: 已恢复 初稿=未审核 筛选")
            except Exception:
                pass


# ---------------------------------------------------------------- 下载
def try_download(d, href, name):
    """带会话Cookie下载初稿到 DOWN_DIR, 返回本地路径(失败None)。"""
    try:
        import urllib.request
        cookies = "; ".join(f"{c['name']}={c['value']}" for c in d.get_cookies())
        req = urllib.request.Request(href, headers={"Cookie": cookies,
                                                    "User-Agent": "Mozilla/5.0"})
        path = os.path.join(DOWN_DIR, f"{name}_{datetime.now():%H%M%S}.docx")
        with urllib.request.urlopen(req, timeout=300) as r, open(path, "wb") as f:
            f.write(r.read(30 * 1024 * 1024))
        return path if os.path.getsize(path) > 10 * 1024 else None
    except Exception as e:
        log.warning(f"下载失败: {type(e).__name__}: {str(e)[:60]}")
        return None


def download_from_modal(d, modal, name):
    """点击最新提交行[下载] → hook window.open 捕获 OSS 直链 → requests(显式禁代理)下载。

    headless 下 window.open 被弹窗拦截(下载不落地, 2026-09-22实测) → 改为捕获直链后
    由 Python 直接 GET; OSS 直链需显式 proxies=None 否则 requests 连接超时。
    最新提交 = 详情表格最后一行(时间升序: 最早在顶, 最新在底)。返回本地路径(失败None)。"""
    try:
        trs = modal.find_elements(By.CSS_SELECTOR, "table tbody tr")
        tr = trs[-1] if trs else None
        links = (tr.find_elements(By.XPATH, './/a[text()="下载"]') if tr else
                 modal.find_elements(By.XPATH, './/a[text()="下载"]'))
        if not links:
            log.warning(f"[{name}] 待审行无[下载]链接")
            return None
        d.execute_script("""
            window.__dlUrls = window.__dlUrls || [];
            if (!window.__origOpenDl) { window.__origOpenDl = window.open; }
            window.open = function(u){ window.__dlUrls.push(u); return {closed:false}; };
        """)
        js_click(d, links[0])
        time.sleep(2)
        urls = d.execute_script("return window.__dlUrls || [];")
        if not urls:
            log.warning(f"[{name}] 未捕获下载URL")
            return None
        url = urls[-1]
        if not url.lower().startswith("http"):
            log.warning(f"[{name}] 捕获URL非法: {str(url)[:60]}")
            return None
        import requests
        import urllib.parse
        sess = requests.Session()
        for c in d.get_cookies():
            sess.cookies.set(c["name"], c["value"])
        r = sess.get(url, timeout=(15, 300), proxies={"http": None, "https": None})
        r.raise_for_status()
        fn = os.path.join(DOWN_DIR, urllib.parse.unquote(os.path.basename(url.split("?")[0])))
        with open(fn, "wb") as f:
            f.write(r.content)
        if os.path.getsize(fn) > 10 * 1024:
            log.info(f"[{name}] 已下载: {os.path.basename(fn)} ({os.path.getsize(fn)//1024}KB)")
            return fn
        log.warning(f"[{name}] 下载内容过小({os.path.getsize(fn)}B), 丢弃")
        return None
    except Exception as e:
        log.warning(f"[{name}] 下载异常: {type(e).__name__}: {str(e)[:80]}")
        return None


# ---------------------------------------------------------------- 主流程
SCORES_CSV = os.path.join(OUT_DIR, "论文成绩表.csv")
_SCORE_FIELDS = ["姓名", "准考证号", "专业", "答辩方式", "阶段", "论文题目",
                 "是否合格", "成绩", "评审时间", "意见文件", "提交", "提交验证"]


def update_scores_csv(records):
    """结果记录 → 合并入论文成绩表.csv(2026-09新口径):
    只记录合格(判定=通过)学生的成绩; 按 姓名+阶段 去重, 同一学生同一阶段保留最新一条。
    传 records=[] 也会触发对旧表的清洗(删除不合格行+去重)。Excel兼容utf-8-sig。"""
    import csv
    rows = []
    if os.path.exists(SCORES_CSV):
        try:
            with open(SCORES_CSV, encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
        except Exception as e:
            log.warning(f"成绩表读取失败({type(e).__name__}), 重建")
            rows = []
    # 旧表清洗: 只保留 合格 且 成绩非空 且 非(重放)调试数据 的行, 按(姓名,阶段)去重(优先提交成功, 其次最新)
    def _pick(old, new):
        """同一(姓名,阶段)保留策略: 提交成功优先; 若新旧提交状态相同(均成功或均非成功)保留新记录。"""
        if old is None:
            return new
        old_ok = str(old.get("提交") or "") == "成功"
        new_ok = str(new.get("提交") or "") == "成功"
        if old_ok and not new_ok:
            return old  # 旧记录已成功提交, 不被失败的复查覆盖
        return new
    clean = {}
    for r in rows:
        if r.get("是否合格") != "合格":
            continue
        if not str(r.get("成绩") or "").strip():
            continue  # 无成绩的合格记录(如开题/重放)不入成绩表
        if "(重放)" in (r.get("答辩方式") or ""):
            continue  # 调试重放数据不入正式成绩表
        key = (r.get("姓名"), r.get("阶段"))
        clean[key] = _pick(clean.get(key), r)
    add = 0
    for rec in records:
        if rec.get("判定") != "通过":
            continue  # 不合格/未通过 不入成绩表
        if not str(rec.get("成绩") or "").strip():
            continue  # 无成绩不入表
        if "(重放)" in (rec.get("答辩方式") or ""):
            continue
        key = (rec.get("学生"), rec.get("阶段"))
        if key not in clean:
            add += 1
        new_row = {
            "姓名": rec.get("学生", ""),
            "准考证号": rec.get("准考证号", ""),
            "专业": rec.get("专业", ""),
            "答辩方式": rec.get("答辩方式", ""),
            "阶段": rec.get("阶段", ""),
            "论文题目": rec.get("题目", ""),
            "是否合格": "合格" if rec.get("判定") == "通过" else
                       ("不合格" if rec.get("判定") == "不通过" else ""),
            "成绩": str(rec.get("成绩", "") or ""),
            "评审时间": rec.get("时间", ""),
            "意见文件": rec.get("评语文件", ""),
            "提交": "成功" if rec.get("提交") else
                    ("失败" if rec.get("提交") is False else ""),
            "提交验证": rec.get("提交验证", ""),
        }
        clean[key] = _pick(clean.get(key), new_row)
    tmp = SCORES_CSV + ".tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_SCORE_FIELDS)
        w.writeheader()
        for r in clean.values():
            w.writerow(r)
    os.replace(tmp, SCORES_CSV)
    log.info(f"论文成绩表更新: 共{len(clean)}行(新增{add}) → {SCORES_CSV}")
    return add


def save_results(results, path):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)   # 原子写: 断点安全


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=1)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--commit", action="store_true", help="真实提交: 填意见+签名提交+回读验证")
    ap.add_argument("--any", action="store_true", help="调试重放: 不限初稿/待审(仍不提交)")
    ap.add_argument("--defense", default=DEFENSE_MODE,
                    help="答辩方式筛: 现场答辩(默认)/书面答辩/任意")
    ap.add_argument("--scan", action="store_true", help="只扫描列待审清单, 不审核不提交")
    ap.add_argument("--batch", default="", help="切批次(如 '2025年6月毕业'), 默认当前批次")
    ap.add_argument("--only", default="", help="只处理指定姓名(定点验证)")
    ap.add_argument("--stop-on-unpass", action="store_true",
                    help="遇到第一个【不通过】且已真实提交(--commit)后立即停止批量, 等人工确认; "
                         "合格/无法判定不触发停止")
    a = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(DOWN_DIR, exist_ok=True)
    outf = os.path.join(OUT_DIR, f"stage_audit_{datetime.now():%m%d_%H%M}.json")

    d = make_driver()
    results = []
    try:
        login(d)
        d.get(PAGE_URL)
        wait_table(d)
        time.sleep(2)

# 业务口径②: 页面筛选器主动设为 现场答辩 并查询(--any 调试重放也要筛, 否则全量80人定位不到)
        if a.defense != "任意":
            if set_defense_filter(d, a.defense):
                # 业务口径③: 初稿筛选=未审核(仅生产模式; --any调试重放需看全量学生)
                if not a.any:
                    set_stage_filter(d, "初稿", "未审核")
                # 记录查询前表格第0行文本, 用于判断重渲染完成(旧表本就有行, any()会立即通过)
                _before = ""
                try:
                    _r0 = [t for _, t in student_rows(d)]
                    if _r0:
                        _before = _r0[0][:20]
                except Exception:
                    pass
                click_query(d)
                # 查询后表格异步重渲染: 等待首行内容从旧值变为新值(或旧值为空时等到有行)
                for _ in range(16):
                    time.sleep(1.5)
                    _rows_now = [t for _, t in student_rows(d)]
                    if _rows_now:
                        _now0 = _rows_now[0][:20]
                        if (not _before) or (_now0 != _before):
                            break
                log.info(f"页面筛选已设为: {a.defense} 并查询(重渲染等待完成)")

        # 切批次(历史批次可能有待审/漏审初稿)
        if a.batch:
            for sel in d.find_elements(By.CSS_SELECTOR, "div.ant-select-selection__rendered"):
                if re.match(r"^\d{4}", (sel.text or "").strip()) and "毕业" in (sel.text or ""):
                    js_click(d, sel)
                    time.sleep(1)
                    try:
                        opt = d.find_element(By.XPATH,
                            f'//li[contains(@class,"ant-select-dropdown-menu-item") and contains(text(),"{a.batch}")]')
                        js_click(d, opt)
                        time.sleep(1.5)
                        # select值变化不触发查询(2026-09-21实测: 不点查询表格仍是旧批次名单,
                        # 直到任意弹窗操作才重查) → 必须主动点[查 询]
                        for b in d.find_elements(By.CSS_SELECTOR, "button"):
                            if (b.text or "").replace(" ", "") == "查询":
                                js_click(d, b)
                                break
                        time.sleep(3)
                        log.info(f"已切批次并查询: {a.batch}")
                    except Exception as e:
                        opts = [o.text.strip() for o in d.find_elements(
                            By.CSS_SELECTOR, "li.ant-select-dropdown-menu-item") if o.text.strip()]
                        log.error(f"批次[{a.batch}]未找到。可用: {opts}")
                    break
            wait_table(d, timeout=20)

        audited = skipped_defense = skipped_stage = 0
        stop_on_unpass = False
        for _page in range(a.pages):
            # 只取文本快照; 处理时按索引重新定位行(表格重渲染会导致持有的tr引用stale)
            snap = [t for _, t in student_rows(d)]
            # 2026-09-22修: 查询后表格偶发渲染失败(snap空, 重渲染等待24s仍无行) → 重试点查询+等待重读
            for _try in range(3):
                if snap:
                    break
                log.warning(f"第{_page+1}页快照为空(第{_try+1}次重试), 重新点击[查询]并等待")
                click_query(d)
                time.sleep(5)
                snap = [t for _, t in student_rows(d)]
            log.info(f"第{_page+1}页学生数: {len(snap)}")
            for row_index, row_text in enumerate(snap):
                if audited >= a.limit or stop_on_unpass:
                    break
                name = row_text.split()[0] if row_text else "?"
                major = parse_major(row_text)
                zhunkao = ""
                mzk = re.search(r"\d{12,}", row_text)
                if mzk:
                    zhunkao = mzk.group(0)
                defense = "现场答辩" if "现场答辩" in row_text else ("书面答辩" if "书面答辩" in row_text else "?")
                if a.only and name != a.only:
                    continue
                try:
                    # 业务口径②: 答辩方式筛(默认现场答辩; --defense 可改/任意)
                    if a.defense != "任意" and a.defense not in row_text and not a.any:
                        skipped_defense += 1
                        continue
                    # 按姓名现场定位该行"查看"(抗表格重渲染stale), 带重试
                    views = []
                    for _try in range(3):
                        views = d.find_elements(
                            By.XPATH, f'//tbody/tr[.//td[contains(text(),"{name}")]]//a[text()="查看"]')
                        if views:
                            break
                        time.sleep(2)
                    if not views:
                        log.warning(f"[{name}] 行定位失败(表格重渲染?), 跳过")
                        continue
                    try:
                        js_click(d, views[0])
                    except Exception:
                        time.sleep(2)
                        views = d.find_elements(
                            By.XPATH, f'//tbody/tr[.//td[contains(text(),"{name}")]]//a[text()="查看"]')
                        if not views:
                            continue
                        js_click(d, views[0])
                    modal = None
                    log.info(f"[{name}] 已点查看, 等待详情弹窗")
                    for _ in range(8):
                        time.sleep(1.5)
                        modal = last_modal(d)
                        if modal and "查看阶段详情" in (modal.text[:30] or ""):
                            break
                    if not modal:
                        log.warning(f"[{name}] 详情弹窗未开")
                        close_all_modals(d)
                        continue
                    # 详情弹窗: 新版=记录表格(时间升序, 最新提交在底); 待审=最后一行状态"未审核"
                    trs = modal.find_elements(By.CSS_SELECTOR, "table tbody tr")
                    st_got = None
                    if trs and not a.any:
                        tcells = [c.text.strip() for c in trs[-1].find_elements(By.CSS_SELECTOR, "td")]
                        if len(tcells) >= 3 and tcells[2] == "未审核":
                            st_got = {"stage": TARGET_STAGE, "status": "未审核", "pending": True}
                    if st_got is None:
                        # 老版弹窗文本兜底 / --any 调试重放
                        stages = parse_stages(modal.text)
                        if a.any:
                            st_got = stages[:1] if stages else {"stage": TARGET_STAGE, "status": "", "pending": True}
                        else:
                            cand = [s for s in stages if s["stage"] == TARGET_STAGE and s["pending"]]
                            st_got = cand[0] if cand else None
                    if not st_got:
                        skipped_stage += 1
                        close_all_modals(d)
                        continue
                    st = st_got
                    log.info(f"[{name}] 详情弹窗{len(trs)}行, 状态={st['status']}, 定位审核链接")
                    # 审核链接: 新版=表格最后一行操作列(最后一列td)内[审核]; 老版=“审核初稿”
                    link = []
                    if trs:
                        link = trs[-1].find_elements(By.XPATH, './/td[last()]//a[contains(text(),"审核")]')
                    if not link:
                        link = modal.find_elements(By.XPATH, f'.//a[contains(text(),"审核{st["stage"]}")]')
                    if not link and a.any:
                        # 已审核学生(审核不通过)弹窗只有"审核"链接, 无"审核初稿"
                        link = modal.find_elements(By.XPATH, './/a[contains(text(),"审核")]')
                    if not link:
                        close_all_modals(d)
                        continue
                    # 初稿docx下载: 详情弹窗内[下载]链接(JS触发无href), 取最后一条=最新提交
                    docx_path = None
                    if st["stage"] in ("初稿", "复稿", "终稿"):
                        docx_path = download_from_modal(d, modal, name)
                    log.info(f"[{name}] docx={bool(docx_path)}, 点审核链接")
                    js_click(d, link[0])
                    time.sleep(3)
                    m2 = last_modal(d)
                    if not m2:
                        close_all_modals(d)
                        continue
                    content = m2.text
                    tm = re.search(r"论文题目\s*\n?\s*(\S[^\n]{5,60})", content)
                    title = tm.group(1).strip() if tm else ""
                    passed, opinion, full_report, score = review_draft(title, content, docx_path,
                                                                        web_major=major)
                    verdict = {True: "通过", False: "不通过", None: "无法判定"}[passed]
                    rec = {"学生": name, "准考证号": zhunkao, "专业": major,
                           "答辩方式": defense if not a.any else "(重放)",
                           "阶段": st["stage"], "题目": title, "判定": verdict,
                           "意见": opinion, "成绩": score, "docx": bool(docx_path),
                           "时间": datetime.now().strftime("%m-%d %H:%M"),
                           "提交": None, "提交验证": ""}
                    if full_report:
                        rp = os.path.join(OUT_DIR, f"{name}_{st['stage']}_评语.txt")
                        open(rp, "w", encoding="utf-8").write(full_report)
                        rec["评语文件"] = os.path.basename(rp)
                    # 提交(--commit 且判定明确 且非重放): 结果+意见附件(剥分)
                    if a.commit and passed is not None and not a.any:
                        log.info(f"[{name}] 判定={verdict} 分数={score}, 进入真实提交")
                        ok, msg = submit_audit(d, m2, name, passed, strip_score(opinion))
                        if ok:
                            vok, vmsg = verify_saved(d, name, st["stage"], strip_score(opinion)[:10])
                            rec["提交"] = vok
                            rec["提交验证"] = vmsg
                            log.info(f"[{name}] {msg} | {vmsg}")
                        else:
                            rec["提交"] = False
                            rec["提交验证"] = msg
                    if a.scan:
                        results.append(rec)
                        save_results(results, outf)
                        log.info(f"[扫描] {name}({defense}) {st['stage']}待审 | {title[:30]}")
                        audited += 1
                        close_all_modals(d)
                        continue
                    results.append(rec)
                    save_results(results, outf)   # 每条原子落盘
                    log.info(f"[{name}·{st['stage']}] {verdict} | {title[:30]} | {opinion[:50]}")
                    audited += 1
                    # 确认不合格: 真实提交完成后必须停下等老刘人工确认(验证上传闭环)
                    if a.stop_on_unpass and a.commit and passed is False:
                        stop_on_unpass = True
                        log.info(f"[{name}] 判定=不合格 且已提交 → --stop-on-unpass 触发, 批量停止")
                        break
                    close_all_modals(d)
                except Exception as e:
                    log.warning(f"[{name}] 处理异常: {type(e).__name__}: {str(e)[:80]}")
                    close_all_modals(d)
            if audited >= a.limit or stop_on_unpass:
                break
            try:
                nxt = wait_clickable(d, (By.CSS_SELECTOR, "li.ant-pagination-next"), 10)
                if "disabled" in (nxt.get_attribute("class") or ""):
                    break
                js_click(d, nxt)
                time.sleep(3)
            except Exception:
                break
        n_ok = sum(1 for r in results if r["判定"] == "通过")
        n_sub = sum(1 for r in results if r.get("提交"))
        try:
            update_scores_csv(results)
        except Exception as e:
            log.warning(f"成绩表更新异常: {type(e).__name__}: {str(e)[:60]}")
        mode = "COMMIT(已提交)" if a.commit else "dry-run(零写)"
        print(f"\n初稿审核[{mode}]: 非现场答辩跳过{skipped_defense} | 无待审初稿跳过{skipped_stage} | "
              f"审核{len(results)}条 通过{n_ok} 提交成功{n_sub} → {outf}")
        if stop_on_unpass:
            print("⛔ --stop-on-unpass 触发: 已出现【不合格】且完成提交, 停止后续学生, 等老刘人工确认上传闭环")
    finally:
        d.quit()


if __name__ == "__main__":
    main()
