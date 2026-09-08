# thesis-audit — 论文审题 + 论文审核工具链

自 [thesis-reviser2](https://github.com/itsnone-liu/thesis-reviser2) 抽出的**纯审核线**：
不含论文生成与渲染管线，只做"审题 → 审核 → 返修 → 闭合"四件事。
可在任意装有 python-docx 的机器上独立运行。

## 能力地图

| 层 | 工具 | 干什么 | 输入 → 输出 |
|---|---|---|---|
| 审题 | `audit_llm.py` | LLM 语义复审：题目-内容匹配/跑题章节/摘要质量/结论呼应/参考文献异常/硬伤抽查。**片段级证据**（evidence_scope=片段），任何 pass 只代表"片段未见问题" | docx 清单 → JSON 逐篇结论 |
| 审核·TXT | `audit_txt.py` | 确定性 T1–T10：标签格式/自报清单对账/占位承诺/图注锚点，只报告不改写 | txt → hard/warn |
| 审核·DOCX | `audit_final.py` | 无源终审 D1–D8：目录完整性/标题编号/图表注对账/引用合法/重复段/字数 | docx → hard/warn |
| 审核·对账 | `audit_docx.py` | TXT+DOCX 成对审计 + **渲染回执契约**（缺回执/回执损坏/verdict非pass = ❌） | txt+docx → 对账行 |
| 审核·深审 | `audit_deep.py` | 编排器：TXT旁车回溯 + audit_final 继承 + 逐篇 CSV | docx 清单 → 深审 CSV |
| 返修 | `repair_pipeline.py` | 数值统一/方向词/语义修复 + 修后 LLM 复审迭代；未知专业目录显式失败 | docx → 修复+复审 |
| 审批 | `approve_receipt.py` | 人工批准回执降级项（占位图/fallback），批准后 ❌→⚠️ 且留痕 | docx → 回执盖章 |
| 闭合 | `audit_closure.py` | 清单=盘上=审计三集合对账 + 五类缺项；非0退出码供 cron | 清单+CSV → 闭合表 |

支撑模块：`tagguard.py`（标签守卫）、`civil_consistency.py` / `mechanical_consistency.py`
（专业一致性门）、`core.py`（自渲染仓 vendored 的三个宽容提取函数，仅 audit_docx 使用）。

## 快速开始

```bash
pip install -r requirements.txt
cp .env.example .env   # 填 DASHSCOPE_API_KEY(百炼, 审题用; 可选 CODEX_PROXY_API_KEY 备用通道)

# 深审一批docx(审核主线)
python3 audit_deep.py --list 清单.json --root 论文目录 --csv 审计报告.csv

# LLM审题(片段级语义)
python3 audit_llm.py --list 清单.json --root 论文目录 --out 审题结论.json

# 全量闭合(审计完成度对账)
python3 audit_closure.py --root 论文目录 --list 清单.json --audit-csv 审计报告.csv

# 单篇返修
python3 repair_pipeline.py --docx 论文.docx --type 土木

# 人工批准占位图
python3 approve_receipt.py 论文.docx --field placeholder --by 批准人 --reason "示意图已确认"
```

清单 json 为相对路径数组：`["25/经管/张三_xxx.docx", ...]`。

## 设计原则（继承自 thesis-reviser）

1. **失败必须响亮**：守卫/审计异常直接 raise 或记 ❌，禁止 except-pass 把
   "没检查成"伪装成"通过"。
2. **回执即契约**：渲染产物必须带 `.report.json` 回执；缺回执、回执损坏、
   verdict 非 pass 一律 ❌；人工批准是唯一降级通道且全量留痕。
3. **审与修分离**：审计层只报告不改写；改写集中在 repair_pipeline，
   修后必须复审。
4. **证据范围显式**：LLM 审题每条记录标 evidence_scope=片段 + fragments
   统计；retry/error 不进 done 集合，重跑必须重试。
5. **闭合可机械验证**：审计覆盖度不靠口头"全审过了"，靠
   清单=盘上=审计 三集合对账 + 退出码。

## 与 thesis-reviser2 的关系

- 上游是完整管线（生成/渲染/交付）；本仓库只取审核线，便于在数据侧
  独立部署。
- `core.py` 为 vendored 摘录（上游 514–872 行的三个宽容提取函数），
  上游变更时需手工同步。
- `audit_llm.py` 除 .env 探测路径本地化外与上游保持一致。

## 依赖

python-docx、Pillow、requests（审题通道）——见 requirements.txt。
