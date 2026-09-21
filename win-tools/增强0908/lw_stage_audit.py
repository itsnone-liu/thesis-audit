# -*- coding: utf-8 -*-
"""lw_stage_audit.py — 网站论文初稿审核程序 v2 (2026-09-21, 服务器化+业务口径修正)。

业务口径(用户 2026-09-21 定):
  ① 只筛 [初稿已提交且未审核] 的学生
  ② 只审 [现场答辩] 的论文(书面答辩不审)
  ③ --commit 真实提交: 弹窗填审核意见 → 签名并提交 → 重读阶段详情验证状态落库

服务器化优化(功能不变):
  - headless + page_load_strategy=eager(DOM就绪即续, 不等图片资源)
  - 禁图片加载(弹窗纯文本渲染, 减带宽/内存)
  - 条件等待替代固定sleep(WebDriverWait), 固定sleep仅兜底
  - chromedriver 由 Selenium Manager/系统路径解析(不依赖Windows chromedriver.exe)

用法:
  python3 lw_stage_audit.py                    # dry-run: 筛查+审核+意见, 零写操作
  python3 lw_stage_audit.py --commit           # 真实提交(意见+签名, 逐条回读验证)
  python3 lw_stage_audit.py --pages 2 --limit 10
  python3 lw_stage_audit.py --any --limit 1    # 调试重放(不限初稿/待审, 不提交)
"""
import os
import re
import sys
import json
import time
import argparse
import logging
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("stage_audit")

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "../批量"))
_SRC = ""  # 凭据走环境变量 ST_USER/ST_PASS, st.py 不入库
LOGIN_URL = os.environ.get("ST_LOGIN_URL", "https://zk.wencaischool.net/#/login")
PAGE_URL = "https://zk.wencaischool.net/#/thesisAssignStu"
USERNAME = os.environ.get("ST_USER", "")
PASSWORD = os.environ.get("ST_PASS", "")

_ENVF = "/root/project/workspace/thesis-reviser/.env"
if os.path.exists(_ENVF):
    for line in open(_ENVF):
        line = line.strip()
        if line.startswith("export "):
            line = line[7:]
        if "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))

TARGET_STAGE = "初稿"          # 业务口径①: 只审初稿
DEFENSE_MODE = "现场答辩"       # 业务口径②: 只审现场答辩
STATUS_WORDS = ("审核通过", "审核不通过", "未审核", "待审核", "已退回", "退回修改")
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
    return webdriver.Chrome(options=opts)


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


def login(d):
    d.get(LOGIN_URL)
    WebDriverWait(d, 20).until(EC.presence_of_element_located(
        (By.CSS_SELECTOR, 'input[placeholder="请输入学号/证件号"]')))
    d.find_element(By.CSS_SELECTOR, 'input[placeholder="请输入学号/证件号"]').send_keys(USERNAME)
    d.find_element(By.CSS_SELECTOR, 'input[placeholder="请输入密码"][type="password"]').send_keys(PASSWORD)
    cb = d.find_element(By.CSS_SELECTOR, 'input[name="type"][type="checkbox"]')
    if not cb.is_selected():
        cb.click()
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
    rows = []
    for tr in d.find_elements(By.CSS_SELECTOR, "tbody tr"):
        t = tr.text.strip()
        if t and "暂无" not in t:
            rows.append((tr, t))
    return rows


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
def review_draft(title, content_text, docx_path=None, web_major=""):
    """初稿审核: 有docx走评审器(新报告格式), 否则弹窗文本走LLM。返回(通过, 意见, 报告全文)。"""
    if docx_path:
        try:
            return review_docx(docx_path, title, web_major=web_major)
        except Exception as e:
            log.warning(f"docx评审失败({type(e).__name__}), 回退LLM文本审: {str(e)[:60]}")
    passed, comment = llm_review_text(title, content_text)
    return passed, comment, None


def review_docx(path, title_hint="", web_major=""):
    """下载的初稿docx → 选评审器(专业以网页列表为准, 封面仅兜底) → (通过, 意见, 报告)。"""
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
    else:
        from lwsj import ThesisDesignReviewer
        rv = ThesisDesignReviewer()
    out = rv.review_thesis(path)
    r = out["result"]
    import report_render as RR
    report = RR.render(out)
    passed = bool(r.get("是否合格"))
    # 意见=报告的总体评价段(给网站存档)
    m = re.search(r"【总体评价】(.+?)(?=\n【具体评价】|$)", report, re.S)
    opinion = (m.group(1).strip() if m else str(r.get("总体评语", "")))[:450]
    return passed, opinion, report


def llm_review_text(title, content_text):
    """弹窗文本LLM审(初稿无docx时的兜底)。"""
    import requests
    url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1") + "/chat/completions"
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
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
def submit_audit(d, modal, opinion):
    """弹窗内: 填意见 → 签名并提交 → 处理可能出现的确认框。"""
    ta = modal.find_elements(By.CSS_SELECTOR, "textarea")
    if not ta:
        return False, "未找到意见输入框"
    ta[0].clear()
    ta[0].send_keys(opinion)
    time.sleep(0.5)
    btn = None
    for b in modal.find_elements(By.CSS_SELECTOR, "button.ant-btn-primary"):
        if "签名并提交" in (b.text or ""):
            btn = b
            break
    if not btn:
        return False, "未找到[签名并提交审核结果]按钮"
    js_click(d, btn)
    time.sleep(2)
    # 可能的二次确认(ant-modal-confirm 的 确定)
    for _ in range(2):
        try:
            ok = d.find_element(By.CSS_SELECTOR, ".ant-modal-confirm-btns .ant-btn-primary")
            js_click(d, ok)
            time.sleep(1.5)
        except Exception:
            break
    time.sleep(2)
    # 弹窗成功反馈检测(用户2026-09-21: 上传成功弹窗应有显示)
    for _ in range(6):
        try:
            msgs = d.find_elements(By.CSS_SELECTOR, ".ant-message-notice, .ant-message")
            for m in msgs:
                t = m.text.strip()
                if t:
                    return True, f"弹窗反馈: {t[:40]}"
        except Exception:
            pass
        # 弹窗自行关闭=提交生效的常见表现
        if not [x for x in d.find_elements(By.CSS_SELECTOR, ".ant-modal") if x.is_displayed()]:
            return True, "弹窗已关闭(提交生效表现)"
        time.sleep(1)
    return True, "已点击提交(未见明确反馈, 以回读为准)"


def verify_saved(d, name, stage, opinion_head):
    """回读验证: 重开该生阶段详情, 确认审核状态已变+意见已存。"""
    try:
        close_all_modals(d)
        time.sleep(1)
        views = d.find_elements(
            By.XPATH, f'//tbody/tr[.//td[contains(text(),"{name}")]]//a[text()="查看"]')
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
        stages = parse_stages(txt)
        target = [s for s in stages if s["stage"] == stage]
        if not target:
            return False, "回读: 阶段行未找到"
        st = target[0]
        if st["status"] in ("审核通过", "审核不通过"):
            saved_op = opinion_head[:10] in txt
            return True, f"回读验证: {st['status']}" + ("+意见已存" if saved_op else "(意见未见, 请人工核对)")
        return False, f"回读: 状态仍为[{st['status'] or '空'}], 提交可能未生效"
    except Exception as e:
        return False, f"回读异常: {type(e).__name__}: {str(e)[:60]}"


# ---------------------------------------------------------------- 下载
def try_download(d, href, name):
    """带会话Cookie下载初稿到 DOWN_DIR, 返回本地路径(失败None)。"""
    try:
        import urllib.request
        cookies = "; ".join(f"{c['name']}={c['value']}" for c in d.get_cookies())
        req = urllib.request.Request(href, headers={"Cookie": cookies,
                                                    "User-Agent": "Mozilla/5.0"})
        path = os.path.join(DOWN_DIR, f"{name}_{datetime.now():%H%M%S}.docx")
        with urllib.request.urlopen(req, timeout=60) as r, open(path, "wb") as f:
            f.write(r.read(30 * 1024 * 1024))
        return path if os.path.getsize(path) > 10 * 1024 else None
    except Exception as e:
        log.warning(f"下载失败: {type(e).__name__}: {str(e)[:60]}")
        return None


# ---------------------------------------------------------------- 主流程
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
        for _page in range(a.pages):
            # 只取文本快照; 处理时按索引重新定位行(表格重渲染会导致持有的tr引用stale)
            snap = [t for _, t in student_rows(d)]
            log.info(f"第{_page+1}页学生数: {len(snap)}")
            for row_index, row_text in enumerate(snap):
                if audited >= a.limit:
                    break
                name = row_text.split()[0] if row_text else "?"
                major = parse_major(row_text)
                defense = "现场答辩" if "现场答辩" in row_text else ("书面答辩" if "书面答辩" in row_text else "?")
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
                    for _ in range(8):
                        time.sleep(1.5)
                        modal = last_modal(d)
                        if modal and "查看阶段详情" in (modal.text[:30] or ""):
                            break
                    if not modal:
                        log.warning(f"[{name}] 详情弹窗未开")
                        close_all_modals(d)
                        continue
                    stages = parse_stages(modal.text)
                    # 业务口径①: 初稿已提交且未审核(a.any 调试重放开第一个阶段)
                    if a.any:
                        cand = stages[:1]
                    else:
                        cand = [s for s in stages if s["stage"] == TARGET_STAGE and s["pending"]]
                    if not cand:
                        skipped_stage += 1
                        close_all_modals(d)
                        continue
                    st = cand[0]
                    link = modal.find_elements(By.XPATH, f'.//a[contains(text(),"审核{st["stage"]}")]')
                    if not link:
                        close_all_modals(d)
                        continue
                    js_click(d, link[0])
                    time.sleep(3)
                    m2 = last_modal(d)
                    if not m2:
                        close_all_modals(d)
                        continue
                    content = m2.text
                    tm = re.search(r"论文题目\s*\n?\s*(\S[^\n]{5,60})", content)
                    title = tm.group(1).strip() if tm else ""
                    # 初稿docx下载探测(有下载链接则下载走评审器)
                    docx_path = None
                    if st["stage"] in ("初稿", "复稿", "终稿"):
                        for dl in m2.find_elements(By.CSS_SELECTOR, "a[href]"):
                            href = dl.get_attribute("href") or ""
                            if any(ext in href.lower() for ext in (".doc", ".docx", "download", "file")):
                                docx_path = try_download(d, href, name)
                                break
                    passed, opinion, full_report = review_draft(title, content, docx_path,
                                                                  web_major=major)
                    verdict = {True: "通过", False: "不通过", None: "无法判定"}[passed]
                    rec = {"学生": name, "专业": major, "答辩方式": defense if not a.any else "(重放)",
                           "阶段": st["stage"], "题目": title, "判定": verdict,
                           "意见": opinion, "docx": bool(docx_path),
                           "时间": datetime.now().strftime("%m-%d %H:%M"),
                           "提交": None, "提交验证": ""}
                    if full_report:
                        rp = os.path.join(OUT_DIR, f"{name}_{st['stage']}_评语.txt")
                        open(rp, "w", encoding="utf-8").write(full_report)
                        rec["评语文件"] = os.path.basename(rp)
                    # 提交(--commit 且判定明确 且非重放)
                    if a.commit and passed is not None and not a.any:
                        ok, msg = submit_audit(d, m2, opinion)
                        if ok:
                            vok, vmsg = verify_saved(d, name, st["stage"], opinion)
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
                    close_all_modals(d)
                except Exception as e:
                    log.warning(f"[{name}] 处理异常: {type(e).__name__}: {str(e)[:80]}")
                    close_all_modals(d)
            if audited >= a.limit:
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
        mode = "COMMIT(已提交)" if a.commit else "dry-run(零写)"
        print(f"\n初稿审核[{mode}]: 非现场答辩跳过{skipped_defense} | 无待审初稿跳过{skipped_stage} | "
              f"审核{len(results)}条 通过{n_ok} 提交成功{n_sub} → {outf}")
    finally:
        d.quit()


if __name__ == "__main__":
    main()
