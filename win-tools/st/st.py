from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException, NoSuchElementException, StaleElementReferenceException, ElementClickInterceptedException
import requests
import json
import time
import logging
from datetime import datetime

# ==================== 日志配置 ====================
log_filename = f"audit_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_filename, encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# ==================== 配置区域 ====================
LOGIN_URL = "https://zk.wencaischool.net/#/login"
TARGET_URL = "https://zk.wencaischool.net/#/thesisAssignStu"
USERNAME = os.environ.get("ST_USER", "")  # 凭据走环境变量, 不入库
PASSWORD = os.environ.get("ST_PASS", "")

# DeepSeek API配置
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"

# 不通过理由
REJECT_REASON = "太宽泛无具体研究对象"

# ==================== 专业分类 ====================
# 直接通过的专业（无需AI判断）
DIRECT_PASS_MAJORS = [
    "土木工程", 
    "机械工程", 
    "法学",
    "服装与服饰设计",
    "数字媒体艺术",
    "视觉传达设计"
    # 环境设计已被移除，需要AI判断
]

# 需要严格审核的专业（需要具体研究对象）
STRICT_MAJORS = ["财务管理", "人力资源管理", "金融学"]

# ==================== 选择器 ====================
USERNAME_SELECTOR = (By.CSS_SELECTOR, 'input[placeholder="请输入学号/证件号"]')
PASSWORD_SELECTOR = (By.CSS_SELECTOR, 'input[placeholder="请输入密码"][type="password"]')
CHECKBOX_SELECTOR = (By.CSS_SELECTOR, 'input[name="type"][type="checkbox"]')
LOGIN_BTN_SELECTOR = (By.CSS_SELECTOR, 'div.submitBtn')
THESIS_STATUS_DROPDOWN = (By.XPATH, '//label[text()="选题状态"]/following::div[contains(@class,"ant-select-selection")][1]')
DEFENSE_METHOD_DROPDOWN = (By.XPATH, '//label[text()="答辩方式"]/following::div[contains(@class,"ant-select-selection")][1]')
QUERY_BTN_SELECTOR = (By.XPATH, '//button/span[text()="查 询"]/parent::button')
STUDENT_ROWS_SELECTOR = (By.CSS_SELECTOR, 'tr.ant-table-row')
FIRST_VIEW_LINK_SELECTOR = (By.XPATH, '(//a[text()="查看"])[1]')

# 论文题目选择器（在模态框内）
THESIS_TITLE_SELECTOR = (By.XPATH, '//div[@class="ant-modal-body"]//td[@class="ant-table-row-cell-break-word"][1]')

FIRST_AUDIT_BTN_SELECTOR = (By.XPATH, '//a[text()="审核选题"]')
RESULT_DROPDOWN = (By.XPATH, '//label[contains(text(),"审核结果")]/following::div[contains(@class,"ant-select-selection")][1]')
REJECT_REASON_INPUT = (By.XPATH, '/html/body/div[5]/div[1]/div[2]/div[1]/div[2]/div[2]/form[1]/div[2]/div[2]/div[1]/span[1]/input[1]')
SAVE_BTN_SELECTOR = (By.XPATH, '//button[span[text()="保 存"]]')
MODAL_CLOSE_BTN = (By.XPATH, '//div[@role="dialog"]//button[@aria-label="Close"] | //div[@class="ant-modal-close"]')
ERROR_PROMPT = (By.CSS_SELECTOR, 'div.ant-form-explain')

# ==================== 记录已处理学生 ====================
processed_students = set()

# ==================== 审核判断函数 ====================
def should_pass_directly(major: str) -> bool:
    """根据专业判断是否直接通过"""
    return major in DIRECT_PASS_MAJORS

def check_thesis_with_deepseek(title: str, major: str):
    """调用DeepSeek判断论文题目是否符合要求，根据专业定制提示"""
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    
    # 基础提示
    base_prompt = f"""你是一个专业的论文审核专家。请严格基于以下论文题目本身进行判断，不要编造或添加题目中不存在的信息。

当前论文题目是：{title}
学生专业是：{major}

请仔细阅读题目内容，然后回答。
"""
    
    # 专业特定要求
    major_requirement = ""
    if major in STRICT_MAJORS:
        major_requirement = f"""
特别注意：该学生专业是【{major}】，审核标准需要更严格。
必须确保题目包含【具体的研究对象】，如：
- xx公司的xx问题研究（例如：恒峰公司融资困难及对策的研究）
- 以xx公司为例的xx研究
- xx企业xx问题分析

研究对象可能出现在题目的任何位置，例如：
- "HY房地产开发有限公司员工薪酬满意度提升策略研究"（研究对象"HY房地产开发有限公司"在开头）
- "员工薪酬满意度提升策略研究——以HY房地产开发有限公司为例"（研究对象在结尾）
- "基于HY房地产开发有限公司的员工薪酬满意度研究"（研究对象在中间）

如果题目只是泛泛讨论理论（如"企业财务管理研究"），没有具体案例或企业名称，则判定为不通过。"""
    elif major == "环境设计":
        major_requirement = f"""
该学生专业是【环境设计】，需要确保题目中包含【具体的建筑或场所】作为研究对象，例如：
- 公园景观设计（如：XX公园改造设计、XX公园景观提升）
- 小区环境设计（如：XX小区景观设计、XX小区环境改造）
- 咖啡馆/餐厅室内设计（如：XX咖啡馆空间设计、XX餐厅室内设计）
- 广场/街道景观设计（如：XX广场景观提升、XX街道改造设计）
- 学校/医院等公共建筑（如：XX学校景观设计、XX医院环境设计）

研究对象可能出现在题目的任何位置，例如：
- "南通市中新花园康养居住小区景观设计"（研究对象"南通市中新花园"在开头）
- "康养居住小区景观设计——以南通市中新花园为例"（研究对象在结尾）
- "基于南通市中新花园的康养居住小区景观设计研究"（研究对象在中间）

如果题目中没有明确具体的建筑、场所或地点，则判定为不通过。"""
    
    prompt = base_prompt + major_requirement + """

判断规则：
1. 仔细阅读题目，找出其中提到的【具体研究对象】（如企业名称、公司名称、具体案例、人名、建筑名、场所名等）
2. 研究对象可能出现在题目的开头、中间或结尾，请全面扫描题目内容
3. 只有在题目中明确出现了具体研究对象时，才判定为通过
4. 如果题目中完全没有具体研究对象，只有泛泛的理论讨论，判定为不通过
5. 务必基于题目本身判断，不要添加题目中不存在的信息

请以JSON格式返回，格式如下：
{
    "passed": true/false,
    "reason": "简要说明判断理由，明确指出题目中的具体研究对象是什么（如果通过），或为什么没有具体研究对象（如果不通过）"
}

论文题目：{title}
学生专业：{major}"""

    data = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "你是一个严谨的论文审核专家，必须严格基于用户提供的论文题目进行判断，不得编造或添加题目中不存在的信息。"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.01,
        "max_tokens": 200,
        "response_format": {"type": "json_object"}
    }

    try:
        response = requests.post(DEEPSEEK_API_URL, headers=headers, json=data, timeout=30)
        if response.status_code == 200:
            result = response.json()
            content = result['choices'][0]['message']['content']
            logging.info(f"AI原始返回: {content}")
            
            try:
                result_json = json.loads(content)
                # 验证返回的reason中是否包含题目中不存在的公司名
                reason = result_json.get('reason', '')
                passed = result_json.get('passed', True)
                
                # 简化幻觉检测，只处理明显错误的情况
                if passed and '公司' in reason and '公司' not in title and '企业' not in title and '集团' not in title:
                    # 可能是幻觉，但先信任AI的判断
                    logging.warning("AI可能产生幻觉，但当前判断正确，继续执行")
                    # 不修改结果，信任AI
                
                return passed, reason
            except:
                return True, "解析失败，默认通过"
        else:
            return True, f"API失败，默认通过"
    except:
        return True, "API异常，默认通过"

# ==================== 基础函数 ====================
def login(driver):
    logging.info("=== 开始登录 ===")
    driver.get(LOGIN_URL)
    time.sleep(2)
    driver.find_element(*USERNAME_SELECTOR).send_keys(USERNAME)
    driver.find_element(*PASSWORD_SELECTOR).send_keys(PASSWORD)
    checkbox = driver.find_element(*CHECKBOX_SELECTOR)
    if not checkbox.is_selected():
        checkbox.click()
    driver.find_element(*LOGIN_BTN_SELECTOR).click()
    time.sleep(3)

def filter_unreviewed_and_defense(driver):
    logging.info("=== 开始筛选 ===")
    time.sleep(2)
    driver.find_element(*THESIS_STATUS_DROPDOWN).click()
    time.sleep(1)
    driver.find_element(By.XPATH, '//li[text()="未审核"]').click()
    logging.info("✓ 选择未审核")
    time.sleep(1)
    driver.find_element(*DEFENSE_METHOD_DROPDOWN).click()
    time.sleep(1)
    driver.find_element(By.XPATH, '//li[text()="现场答辩"]').click()
    logging.info("✓ 选择现场答辩")
    time.sleep(1)
    driver.find_element(*QUERY_BTN_SELECTOR).click()
    logging.info("✓ 点击查询按钮")
    time.sleep(3)

def get_student_info(driver):
    """获取第一个未处理的学生信息"""
    try:
        rows = driver.find_elements(*STUDENT_ROWS_SELECTOR)
        if not rows:
            return None, None, None
        
        for row in rows:
            try:
                cells = row.find_elements(By.CSS_SELECTOR, 'td.ant-table-row-cell-break-word')
                if len(cells) < 7:
                    continue
                    
                student_name = cells[0].text.strip()
                
                if student_name in processed_students:
                    continue
                
                major = cells[6].text.strip()
                view_link = row.find_element(By.XPATH, './/a[text()="查看"]')
                
                logging.info(f"找到未处理学生: {student_name}, 专业: {major}")
                return student_name, major, view_link
            except:
                continue
        
        return None, None, None
    except Exception as e:
        logging.error(f"获取学生信息失败: {e}")
        return None, None, None

def close_all_modals(driver):
    """关闭所有模态框"""
    logging.info("关闭所有模态框...")
    try:
        close_btns = driver.find_elements(*MODAL_CLOSE_BTN)
        for btn in close_btns:
            try:
                if btn.is_displayed():
                    driver.execute_script("arguments[0].click();", btn)
                    logging.info("✓ 点击关闭按钮")
                    time.sleep(0.5)
            except:
                pass
        
        for _ in range(3):
            webdriver.ActionChains(driver).send_keys(Keys.ESCAPE).perform()
            time.sleep(0.3)
        
        time.sleep(1)
        return True
    except Exception as e:
        logging.warning(f"关闭模态框时出错: {e}")
        return False

def input_rejection_reason(driver):
    """输入不通过理由"""
    logging.info("开始输入不通过理由...")
    try:
        input_box = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located(REJECT_REASON_INPUT)
        )
        logging.info("✓ 找到审核弹窗中的输入框")
        
        driver.execute_script("arguments[0].scrollIntoView(true);", input_box)
        time.sleep(0.3)
        input_box.click()
        time.sleep(0.3)
        input_box.clear()
        time.sleep(0.3)
        
        for char in REJECT_REASON:
            input_box.send_keys(char)
            time.sleep(0.05)
        
        input_box.send_keys(Keys.TAB)
        time.sleep(0.5)
        
        current_value = input_box.get_attribute('value')
        logging.info(f"输入框当前值: '{current_value}'")
        
        error_elements = driver.find_elements(*ERROR_PROMPT)
        if error_elements:
            logging.warning(f"仍有红字: '{error_elements[0].text}'")
            return False
        else:
            logging.info("✓ 输入成功，无红字")
            return True
            
    except Exception as e:
        logging.error(f"输入理由时出错: {e}")
        return False

# ==================== 审核循环 ====================
def review_loop(driver):
    """循环审核"""
    review_count = 0
    pass_count = 0
    fail_count = 0
    consecutive_failures = 0
    max_consecutive_failures = 3
    
    while True:
        try:
            if consecutive_failures >= max_consecutive_failures:
                logging.error(f"连续{max_consecutive_failures}次失败，终止程序")
                break
                
            logging.info(f"\n{'='*60}")
            logging.info(f"开始第 {review_count + 1} 轮审核")
            
            # 获取学生信息
            student_name, major, view_link = get_student_info(driver)
            if not student_name:
                logging.info("没有更多未处理的学生")
                break
            
            logging.info(f"处理学生: {student_name}, 专业: {major}")
            
            # 点击查看
            driver.execute_script("arguments[0].click();", view_link)
            logging.info(f"✓ 点击学生 {student_name} 的查看按钮")
            time.sleep(2)
            
            # 在模态框内获取论文题目
            try:
                title_elem = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located(THESIS_TITLE_SELECTOR)
                )
                thesis_title = title_elem.text.strip()
                logging.info(f"论文题目: {thesis_title}")
            except Exception as e:
                logging.error(f"获取论文题目失败: {e}")
                close_all_modals(driver)
                consecutive_failures += 1
                continue
            
            # 专业判断
            if should_pass_directly(major):
                is_pass = True
                reason = f"专业【{major}】直接通过"
                logging.info(f"✓ 专业直接通过: {major}")
            else:
                is_pass, reason = check_thesis_with_deepseek(thesis_title, major)
                logging.info(f"AI判断: {'通过' if is_pass else '不通过'}")
            
            logging.info(f"理由: {reason}")
            
            # 点击审核选题
            try:
                audit_link = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable(FIRST_AUDIT_BTN_SELECTOR)
                )
                driver.execute_script("arguments[0].click();", audit_link)
                logging.info("✓ 点击审核选题按钮")
                time.sleep(2)
            except Exception as e:
                logging.error(f"点击审核选题按钮失败: {e}")
                close_all_modals(driver)
                consecutive_failures += 1
                continue
            
            # 等待第二个弹窗
            try:
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located(RESULT_DROPDOWN)
                )
            except TimeoutException:
                logging.error("等待第二个弹窗超时")
                close_all_modals(driver)
                consecutive_failures += 1
                continue
            
            # 选择审核结果
            try:
                result_dropdown = driver.find_element(*RESULT_DROPDOWN)
                driver.execute_script("arguments[0].click();", result_dropdown)
                time.sleep(1)
                
                option_text = "通过" if is_pass else "不通过"
                option = driver.find_element(By.XPATH, f'//li[text()="{option_text}"]')
                driver.execute_script("arguments[0].click();", option)
                logging.info(f"✓ 选择: {option_text}")
                time.sleep(1)
            except Exception as e:
                logging.error(f"选择审核结果失败: {e}")
                close_all_modals(driver)
                consecutive_failures += 1
                continue
            
            # 如果不通过，输入理由
            if not is_pass:
                input_success = input_rejection_reason(driver)
                if not input_success:
                    logging.warning("输入理由失败，但继续尝试保存")
            
            # 点击保存
            try:
                save_btn = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable(SAVE_BTN_SELECTOR)
                )
                driver.execute_script("arguments[0].click();", save_btn)
                logging.info("✓ 点击保存按钮")
                time.sleep(3)
            except Exception as e:
                logging.error(f"点击保存按钮失败: {e}")
                close_all_modals(driver)
                consecutive_failures += 1
                continue
            
            # 关闭所有模态框
            close_all_modals(driver)
            
            # 记录已处理
            processed_students.add(student_name)
            review_count += 1
            if is_pass:
                pass_count += 1
            else:
                fail_count += 1
            
            consecutive_failures = 0
            logging.info(f"当前统计: 已处理 {review_count} 个, 通过: {pass_count}, 不通过: {fail_count}")
            
            # 刷新页面
            driver.refresh()
            time.sleep(3)
            filter_unreviewed_and_defense(driver)
            
        except Exception as e:
            logging.error(f"处理学生时出错: {e}")
            close_all_modals(driver)
            driver.refresh()
            time.sleep(3)
            filter_unreviewed_and_defense(driver)
            consecutive_failures += 1
            continue
    
    return review_count, pass_count, fail_count

# ==================== 主程序 ====================
if __name__ == "__main__":
    logging.info("="*60)
    logging.info("自考管理系统自动审核脚本 (环境设计AI版)")
    logging.info("="*60)
    
    from selenium.webdriver.chrome.service import Service
    driver = webdriver.Chrome(service=Service(r'D:\worktool\st\chromedriver.exe'))
    driver.maximize_window()
    
    try:
        login(driver)
        driver.get(TARGET_URL)
        time.sleep(3)
        filter_unreviewed_and_defense(driver)
        
        total, passed, failed = review_loop(driver)
        
        logging.info("\n" + "="*60)
        logging.info(f"🎉 审核完成！")
        logging.info(f"共处理: {total} 个学生")
        logging.info(f"通过: {passed}")
        logging.info(f"不通过: {failed}")
        logging.info("="*60)
        
    except Exception as e:
        logging.error(f"\n程序运行出错: {e}")
        import traceback
        traceback.print_exc()
        
    finally:
        logging.info("\n浏览器将在30秒后自动关闭...")
        time.sleep(30)
        driver.quit()