# 📋 OpsPilot 企业运维助手 - 更新日志

## V2.16.0 (2026-07-20)

**零配置交付、首次配置向导与模块化操作台**

- 新增 bootstrap 启动模式：没有 `config.yaml`、空配置或缺少钉钉凭证时，仍可先启动本机管理台；向导完成后自动继续启动机器人。
- 新增五步首次配置向导，覆盖用途、钉钉、线上或本地 AI、能力模块、负责人和上线体检；草稿可恢复，但 Client Secret/API Key 不写入草稿文件且不通过 API 回显。
- 管理台默认收敛为首页、工单、知识、文件与任务、设置五个入口；原有功能全部保留在二级入口，高级参数默认隐藏。
- 首页改为行动中心，优先展示当前待办、逾期、SLA 风险、通知失败和快捷处理入口；各页面进入时按需加载。
- 新增模块注册与可视化开关，统一管理工单、知识闭环、表格、图片、语音和智能表格；图片/语音开关已接入实际消息处理。
- AI 配置新增 DeepSeek、OpenAI 兼容、Ollama 和本地兼容服务预设；本地服务允许无 Key，且不会发送空的 Authorization 头。
- 首次向导配置的默认 AI 可直接作为图片识别的默认路由；高级模式仍可为图片单独指定模型，未配置可用模型时禁止误启用。
- 新增安全配置包导出、变更预览和导入；默认移除密钥、认证 Header、首次完成标记和本机绝对路径；仅同一服务身份保留目标机已有密钥，换绑时主动清除。
- 凭证按服务商、端点及连接器身份隔离；更换目标不会复用旧密钥，导入前后均写入可追溯配置快照。
- 打包流程不再复制开发环境 `config.yaml`，改用脱敏 `config.example.yaml` 生成发行配置，避免真实密钥和本机路径进入交付目录。
- 配置和管理设置改为进程级互斥、随机临时文件、刷新落盘后原子替换；向导完成以正式配置为提交点，失败时回滚人员设置。
- 新增本机 Host/Origin 边界、端口占用快速失败、供应商换绑清除旧密钥和零配置回归测试。
- 模块开关接入真实运行链路：关闭工单会停止查询、建单、附件挂接、智能表格同步和全部工单定时线程；关闭文件/图片能力时附件会在下载前拒绝。
- 表格模块只有检测到 Node.js 与 `@oai/artifact-tool` 才可启用；智能表格同时要求工单、`dws`、Base ID 和 Table ID。
- 清理源码和当前手册中的企业人员、知识库 ID 与租户默认值；周报接收人为空、每日摘要默认关闭。
- 发行目录采用三文件白名单并对 EXE 格式、体积、凭证、URL 敏感参数和绝对路径做失败即停止的门禁；不会清理含运行数据的既有目录。
- 本地 Whisper 从标准依赖拆分到 `requirements-voice-local.txt`；钉钉原生语音识别不受影响，标准安装和主 EXE 不再拉取 Torch/Whisper。
- 发布门禁通过：163 项单元/回归测试、76 项原工单脚本断言、Python 编译、前端 JavaScript 语法、真实 HTTP 脱敏响应和 EXE 两轮零配置启动全部通过。

详细操作和验收说明见 `docs/首次配置与模块化操作台_V2.16.md`。

## V2.15.0 (2026-07-20)

**企业服务台功能完整度与用户验收闭环**

- 新增可视化服务目录，可配置事件/服务请求、默认分类与优先级、处理组、提交指引、响应/解决目标及审批提示。
- 钉钉新增服务台首页、服务目录、标准服务申请、补充、催办、撤回、确认恢复、评分与重新打开指令；员工只能查看和操作自己的工单。
- 工单数据库升级到 schema v3，旧数据自动迁移；服务项、处理组、SLA 时间点、首次响应、用户确认和满意度均固化到工单快照。
- 建立“标记解决 → 等待用户确认 → 确认关闭/重新打开”的验收流程；仅用户确认的解决方案进入知识候选。
- 区分内部备注、公开回复和系统时间线；员工侧隐藏内部备注，公开回复继续发送钉钉通知。
- 管理操作台新增组合筛选、分页、快捷队列、手工建单、批量操作、当前结果导出及完整服务目录维护。
- 运营驾驶舱新增响应/解决 SLA 达标率、P50/P95、待确认、风险/超时、满意度、重开率、服务需求和人员负载指标。
- 钉钉 AI 表格同步扩展请求类型、服务项、处理组、SLA、首次响应、确认时间和满意度字段。
- 修复并发创建工单编号冲突、员工补充误记为首次响应、已分派工单无法转派、撤回计入已解决、旧日志可见性迁移等问题。
- 管理员改为仅按稳定 user_id/staff_id 授权，并支持通过“人员”页面安全初始化；不再按昵称授权或自动提权首位员工。
- 修复跨管理器并发建单撞号、异常导入编号阻断建单、活跃工单提前归档、撤回/无效工单污染 SLA、历史重开率丢失及服务负责人不自动分派。
- 发布门禁通过：110 项单元/回归测试、76 项原工单脚本断言、Python 编译和前端 JavaScript 语法检查全部通过。

详细使用与验收说明见 `docs/企业服务台_V2.15.md`。

## V2.14.0 (2026-07-17)

**管理操作台完整处理工单**

- 工单详情页新增接单分派、优先级调整、处理记录、开始处理、解决、重新打开、标记无效和关闭操作。
- 添加处理记录时不改变工单状态，排查过程完整进入工单时间线。
- 解决工单前强制填写解决方案，分派前强制选择负责人，关闭和标记无效增加二次确认。
- 管理后台操作继续使用统一状态机，并触发创建者通知和 AI 表格后台同步。
- 工单时间线改为时间与日志编号双重倒序，确保同一秒内的最新操作显示在顶部。
- 新增操作台处理记录、重新打开和无效工单回归测试。

## V2.13.0 (2026-07-17)

**网络状态检测与快速测速**

- 管理后台新增“网络诊断”，展示活动接口、内网 IP、公网 IP、网关和 DNS。
- 新增 DNS 解析、公共 DNS Ping、平均延迟及丢包率检测。
- 新增按需下载测速，使用约 2 MB 小文件进行单线程快速判断，不在后台持续占用带宽。
- 钉钉支持“网络状态”“检测网络”“查 IP”“公网 IP”“测速”等指令。
- 兼容 Windows 多 DNS 地址的 PowerShell JSON 结构，并过滤无效的 169.254 自动地址。
- 新增网络诊断聚合与测速结果回归测试。

## V2.12.0 (2026-07-17)

**工单与钉钉 AI 表格关联**

- 新增“工单智能表格”管理页面，可配置钉钉官方 `dws`、Base ID、Table ID、同步间隔和字段映射。
- 新增连接测试与一键同步；首次创建 AI 表格记录，后续通过本地 recordId 映射更新同一条记录。
- 机器人运行期间按配置周期同步，SQLite 继续作为可靠的工单主数据源。
- Base 或 Table 改变时自动隔离旧映射；单条失败不会中断其余工单，并返回失败明细。
- 新增同步创建和重复更新回归测试。

## V2.11.0 (2026-07-17)

**运行监控、诊断中心与通知重试**

- 新增本地可观测性数据库，统一记录运行事件的类别、操作、结果、耗时、用户和错误摘要。
- 钉钉消息处理增加全链路耗时和异常记录；管理后台工单操作也会写入运行事件。
- 工单状态通知新增送达记录，包含待发送、成功、失败、尝试次数、接收人和失败原因。
- 失败通知保存完整重试载荷，可在管理后台点击“重试”；成功后更新原记录并累计尝试次数。
- 管理后台新增“运行监控”，展示今日事件、错误、平均耗时、运行任务、通知成功/失败数量。
- 新增组件健康检查：钉钉 Stream、钉钉 OpenAPI、IMA、AI API、工单数据库、任务数据库和 Node.js 表格环境。
- 监控页面展示最近运行事件和通知送达记录，并支持按通知状态筛选。
- 监控数据默认保留 30 天并在启动时清理，避免长期运行无限增长。
- 新增通知失败记录与重试、监控统计、健康接口及页面控件测试；63 项回归测试与 76 项工单测试共 139 项通过。

## V2.10.0 (2026-07-17)

**用户工单进度与钉钉状态通知**

- 员工发送 `我的工单` 可查看本人最近工单的状态、负责人和最后更新时间；发送完整工单编号可查看处理日志与解决方案。
- 新增统一工单状态通知组件，通知包含工单编号、标题、当前状态、负责人、操作人、处理说明、解决方案和下一步建议。
- 管理后台执行分派、开始处理、解决和关闭后，会异步私聊通知工单创建者，不阻塞后台操作。
- 管理员在钉钉发送 `接手 TKT-...`、`开始处理 TKT-...` 或 `处理工单 TKT-...`，会自动分派给当前管理员、切换为处理中并通知用户。
- 管理员通过钉钉解决、关闭或重新打开工单时，统一使用新的状态通知格式。
- 通知优先使用创建者 `staff_id`，缺失时使用创建者 ID 兜底；发送失败只记录日志，不影响工单状态流转。
- 新增通知正文、管理员后台通知触发和钉钉接手意图测试；61 项回归测试与 76 项工单测试共 137 项通过。

## V2.9.0 (2026-07-17)

**持久化异步任务中心第一阶段**

- 新增 SQLite 持久化后台任务队列，记录任务编号、类型、所有者、状态、进度、结果、错误和起止时间。
- 表格处理移出钉钉 Stream 消息回调；上传并下达指令后立即返回任务编号，不再阻塞后续员工消息。
- 钉钉支持 `我的任务`、`查询任务 TASK-...` 和 `取消任务 TASK-...`。
- 管理后台新增“任务中心”，可按全部、排队、处理中和失败筛选，运行期间每 5 秒自动刷新。
- 支持取消排队任务；运行中任务进入安全取消状态，在下一个处理阶段停止并清理临时文件。
- 程序正常退出时关闭任务执行器；异常退出或重启后，未完成任务自动标记为“已中断”，历史记录继续保留。
- 修复任务数据库连接未显式关闭导致 Windows 文件句柄占用的问题。
- 新增任务执行、排队取消、历史持久化、重启恢复和后台任务接口测试；58 项回归测试与 76 项工单测试共 134 项通过。

## V2.8.0 (2026-07-17)

**工单数据管理中心**

- 工单工作台新增 CSV 批量导入；支持 UTF-8/UTF-8 BOM，限制 5MB，重复工单号自动跳过。
- 导入过程使用数据库事务，系统异常时整批回滚；缺少标题、状态或优先级错误的行会返回明确提示。
- 新增当前列表及精确搜索结果 CSV 导出，使用 UTF-8 BOM，兼容 Excel 中文显示。
- 新增按工单编号、完整标题、创建人、员工编号、负责人、状态、优先级、分类和会话精确搜索。
- 新增全局操作历史查询，可按工单、动作、操作人和日期范围组合筛选；导入操作也会留下审计日志。
- 为员工编号、负责人、分类及历史动作/操作人增加索引，保证数据增长后的查询速度。
- 管理后台新增搜索工具栏、导入导出按钮和操作历史表格。
- 新增中文 CSV 导入导出、重复跳过、精确搜索、历史审计和页面控件测试；54 项回归测试与 76 项工单测试共 130 项通过。

## V2.7.1 (2026-07-17)

**企业运行性能优化**

- 管理后台复用工单数据库和知识闭环服务，避免每次请求重复初始化数据库和执行完整性检查。
- 工单数据库启用 `synchronous=NORMAL`、内存临时表，并为创建人、状态、会话、跟进时间和日期增加组合索引。
- 日报日期查询由 `LIKE` 扫描改为可使用索引的起止时间范围查询。
- 表格运行环境路径仅校验一次；新增最大并发和排队超时，防止多人同时处理大表耗尽 CPU、内存。
- 生产表格任务不再生成不会发送给用户的预览图片；公式错误扫描继续保留，测试验证仍可开启渲染。
- 管理后台“文件助手”新增最大并发任务与排队等待配置。
- 新增数据库索引和性能配置回归测试；52 项回归测试与 76 项工单测试共 128 项通过。

## V2.7.0 (2026-07-17)

**企业文件助手第二阶段**

- 新增多条件筛选、数字/文本升降序排序、指定工作表处理和工作表清单。
- 新增按字段透视汇总并自动生成原生 Excel 柱状图，输出继续保留源数据工作表。
- 新增手机号、邮箱、身份证及指定字段脱敏，避免敏感信息进入后续流转文件。
- 支持连续上传两份表格，按指定共同键识别新增、删除和字段变更，并生成差异说明。
- 同一用户最多暂存两份表格；第三份会替换最早文件，任务完成或取消后统一清理临时文件。
- 多人并发处理同名文件时使用唯一输出文件名，避免结果互相覆盖；第二份对比文件执行相同的格式、大小校验。
- 完成真实 CSV 到 XLSX 的图表与双表对比渲染验证；51 项回归测试与 76 项工单测试共 127 项通过。

## V2.6.0 (2026-07-17)

**企业文件助手第一阶段**

- 钉钉文件消息新增 `.xlsx`、`.csv`、`.tsv` 表格任务识别，与普通工单附件流程分离。
- 支持表格概览、空值/完整重复行检查、整行或指定列去重、条件筛选及按字段求和汇总。
- 用户上传表格后可继续发送自然语言指令；处理结果始终生成新的 `.xlsx` 并通过钉钉发回，不覆盖原文件。
- 使用 `@oai/artifact-tool` 导入和导出工作簿，生成结果时执行公式错误扫描和工作表渲染验证。
- 输出工作簿统一使用清晰表头、适合的数据格式、自动列宽、冻结首行和隐藏默认网格线。
- 表格任务采用“会话 ID + 用户 ID”隔离，替换文件时自动清理旧临时文件，完成或取消后清理本地数据。
- 管理后台新增“文件助手”，可设置启用状态、文件大小、处理超时、Node.js 与表格运行库路径。
- 默认限制 20MB；不执行宏，不覆盖源文件，当前不支持 `.xls` 旧格式。
- 新增指令解析、用户隔离、后台配置和实际工作簿生成验证；49 项回归测试与 76 项工单测试共 125 项通过。

## V2.5.0 (2026-07-17)

**图片报错识别与每日运维驾驶舱**

- 新增 OpenAI 兼容视觉分析服务，调用后台“图片理解”模型路由识别截图中的系统、窗口、错误码和关键报错文字。
- 图片识别结果会进入现有智能排障链路；同时保留原图片附件，后续自动建单时可一并关联。
- 限制视觉分析图片为 10MB，模型未配置、调用失败或不支持图片时自动回退原附件流程。
- 管理后台新增“每日驾驶舱”，展示今日新增、今日解决、当前待办、逾期、高优先级、平均解决耗时、知识缺口和低效知识来源。
- 驾驶舱新增逾期/高优先级工单、今日重复故障、未解决问题和低效 IMA 来源列表。
- 新增视觉消息格式、驾驶舱聚合与后台接口测试；46 项回归测试与 76 项工单测试共 122 项通过。

## V2.4.0 (2026-07-17)

**企业架构重构第一阶段：会话状态组件化**

- 新增 `modules/conversation_state.py`，集中管理进程内用户短期状态。
- 统一封装“会话 ID + 发送者 ID”用户键，避免各功能重复实现隔离规则。
- AI 回答反馈、临时附件、AI 引擎覆盖和知识效果事件从 `chat_handler.py` 迁移到独立状态组件。
- 集中处理状态 TTL、并发锁、附件替换和知识事件一次性消费，降低消息处理器耦合度。
- 保持 V2.3.1 的钉钉消息、工单、排障、知识闭环和群聊隔离行为不变。
- 新增状态组件测试；43 项回归测试与 76 项工单测试共 119 项通过。

## V2.3.1 (2026-07-17)

**多轮处理与群聊用户隔离修复**

- 修复工单问诊结果处理被错误嵌入智能排障分支的问题；问诊补充完成后现在会直接创建工单。
- 修复智能排障处理补充信息时访问未定义问诊变量、触发内部错误的问题。
- 新增统一用户会话键，由会话 ID 与发送者 ID 共同组成。
- 群聊中的工单问诊、智能排障、反馈、附件暂存、专家模式、AI 引擎覆盖和知识效果事件不再被其他群成员覆盖或消费。
- 首次使用引导改为按群成员记录，避免同群后加入的员工看不到操作提示。
- 新增 4 项完整调度与群聊双用户隔离测试；41 项回归测试与 76 项工单测试共 117 项通过。

## V2.3.0 (2026-07-17)

**IMA 知识闭环与持续学习**

- 新增本地 SQLite 知识闭环库，记录问题、回答、引用的 IMA 知识来源及最终处理结果。
- 新增 IMA 来源效果统计，可查看引用次数、成功次数、失败次数和成功率，定位低效或过时知识。
- 用户确认解决且回答未引用知识库时，自动生成待审核知识候选；管理员解决工单后也会生成候选。
- 未解决问题自动进入待改进池，并关联升级后的工单编号，方便集中复盘。
- 新增知识候选脱敏、相似知识检测、质量初始评分以及草稿、审核通过、驳回、已发布状态流转。
- 管理后台新增“知识闭环”页面，可编辑、审核、驳回、发布候选知识，查看来源指标及未解决问题。
- 知识发布采用 Markdown 导出适配器，形成可人工确认后导入 IMA 的标准文档；当前不伪造未开放的 IMA 写入能力。
- 每次编辑、发布和回滚均保留版本快照，可从管理后台恢复历史版本。
- 修复 Windows 下 SQLite 连接未及时关闭导致临时数据库被占用的问题。
- 37 项回归测试与 76 项工单测试共 113 项通过。

## V2.2.0 (2026-07-16)

**智能问题处置流程**

- 新增问题类型分流：操作咨询、单用户故障、多人故障、服务请求、安全事件和普通问题。
- 安全事件、账号权限和密码类服务请求不再进入普通 AI 排障，直接整理信息并转人工工单。
- 新增动态补问：根据用户已经提供的信息判断缺失项，一次最多询问两个关键问题。
- 显式“报修”问诊同步改为动态模式，不再固定执行三轮问答。
- 信息已经包含设备、时间、尝试操作和影响范围时，直接进入解决方案。
- AI 回答统一包装为“当前判断、处理步骤、预计耗时、风险和反馈选项”。
- 新增会话级排障状态，记录已经执行的方案，下一次建议明确要求避免重复。
- 支持“好了、恢复了、还是不行、没变化、看不到、转人工、取消处理”等自然语言反馈。
- 连续两次方案无效时自动创建工单，并附带原始问题、用户补充、每次排查方案和最终反馈。
- 用户随时可以说“直接报修”或“转人工”跳过自助排障。
- 排障会话默认保留 30 分钟，可通过 `problem_resolution` 配置关闭或调整失败阈值。
- 新增 5 组智能处置状态机测试并更新动态问诊测试；31 项回归测试与 76 项工单测试共 107 项通过。

## V2.1.0 (2026-07-16)

**企业运维工作台与可扩展集成版本**

- 新增可视化工单工作台：全部/处理中/已完成筛选、详情、操作时间线、优先级、分派、开始处理、解决和关闭。
- 新增 HTTP 连接器模板：请求方法、URL 变量、Headers、Bearer/API Key 认证、JSON 请求体和查询参数。
- 新增响应字段映射，支持用 `名称=data.path` 将企业接口响应转换成可读结果。
- 新增连接器执行接口，配置后的 HTTP 连接器能够实际发起请求并返回映射结果。
- 新增多模型配置：每个模型可独立设置地址、接口路径、模型和密钥。
- 新增普通问答、工单分析、图片理解和全局备用模型路由。
- 普通问答已接入多模型路由；首选模型失败时自动尝试备用模型，然后再回退原有默认 API。
- 新增配置版本历史；修改 API、连接器、人员、回答策略或模型前自动保存快照。
- 新增一键回滚，回滚前会再次生成保护快照。
- 模型密钥和连接器认证密钥不会通过管理接口返回明文。
- 新增工单工作台、HTTP 模板映射、密钥脱敏和配置回滚测试；26 项回归测试与 76 项工单测试共 102 项通过。

## V2.0.0 (2026-07-16)

**企业可视化管理平台第一版（无登录、本机访问）**

- 新增浏览器管理后台，EXE 启动后默认自动打开 `http://127.0.0.1:8765`。
- 新增运行总览：消息量、活动会话、连接器、服务人员及核心组件连接状态。
- 新增 AI API 页面：启停、Base URL、接口路径、模型、密钥、温度、Token 和超时配置，并支持在线连接测试。
- API Key 不通过后台接口返回明文，页面仅显示掩码。
- 新增连接器管理：内置钉钉、IMA，并支持添加企业 HTTP 服务及连通性测试。
- 新增人员与职责管理：姓名、钉钉 Staff ID、部门、角色和擅长领域。
- 新增回答策略：助手名称、语气、长度、表情、来源引用、不确定处理、转人工规则和自定义系统指令。
- 回答策略已经接入机器人系统提示词，保存后会影响实际 AI 回答。
- 企业扩展配置独立保存在 `data/admin_settings.json`，与钉钉密钥配置分离。
- 后台只绑定 `127.0.0.1`，不提供登录页，也不对局域网或公网开放。
- 新增后台页面、配置、密钥脱敏和数值配置回归测试；22 项回归测试与 76 项工单测试共 98 项通过。

## V1.8.1 (2026-07-15)

**修复 EXE 控制台无法输入或输入不可见**

- 控制台切换为独立线程内的 Windows 原生 `input()` 输入。
- 新增 `bot>` 提示符，输入内容可见，并正常支持退格、粘贴和中文输入法。
- 输入线程阻塞不会影响 Stream 消息接收与后台任务。
- 新增原生输入回显回归测试；19 项回归测试与 76 项工单测试共 95 项通过。
- 打包产物升级为 `dingtalk-bot-v1.8.1.exe`。

## V1.8.0 (2026-07-15)

**恢复并增强 EXE 控制台，自助配置 OpenAI 兼容 API**

- 新增 `api-config` 控制台命令，可查看、启停及修改 Base URL、接口路径、模型和 API Key。
- API Key 在控制台中始终掩码显示，修改密钥后不回显明文。
- 自定义接口支持单独配置 `chat_completions_path`，默认 `/v1/chat/completions`，可接入 DeepSeek、OpenAI 兼容网关或企业内部模型服务。
- 配置修改仅定点更新 `config.yaml` 的 `llm` 段，保留原文件注释和其他配置。
- 修复 Windows 控制台输入缓冲区每 150ms 被清空的问题，恢复慢速键入命令的可靠识别。
- 打包产物升级为 `dingtalk-bot-v1.8.0.exe`。
- 新增控制台 API 配置回归测试；18 项回归测试与 76 项工单功能测试共 94 项通过。

## V1.7.1 (2026-07-15)

**修复钉钉语音和图片消息收到后无响应**

- 根因：代码读取 `msg.msgtype`，而当前 `dingtalk_stream` SDK 实际属性为 `message_type`，导致非文本消息统一被误判为文本。
- 语音兼容 `audio` 和 `voice` 两种消息类型。
- 当前 SDK 不解析语音内容对象，新增从 `msg.extensions['content']` 提取 `downloadCode`、时长、格式和识别文本。
- 图片改用 SDK 的 `image_content.download_code`，并保留旧字段兼容。
- 钉钉已提供语音识别文本时直接进入消息处理，无需下载或加载 Whisper。
- 未提供识别文本时继续使用 `downloadCode → 下载 → Whisper → 普通消息` 流程。
- 新增真实 SDK 音频和图片反序列化回归测试；全部测试共 93 项通过。

## V1.7.0 (2026-07-15)

**面向减少日常运维工作的自动化版本**

- 新增三轮结构化故障问诊：按设备、ERP、CRM、软件和其他问题分别收集环境、时间、已尝试操作及影响范围。
- 描述达到完整度阈值时直接建单；短描述进入问诊；支持“取消报修”和“直接提交”。
- 新增近期活跃工单相似度检测，同分类相似报障提示重复，累计两条以上标记为疑似群体故障。
- 管理员解决工单后自动设置回访时间，默认 24 小时后私聊用户确认效果。
- 新增“重开 TKT-编号”指令，创建者或管理员可将未解决工单恢复为处理中。
- 新增每天 09:00 个人运维摘要，汇总待处理、高优先级、逾期、等待确认、今日新建和今日解决。
- 数据库 Schema 升级到 `2`，新增回访计划、发送时间和用户确认字段。
- 新增 6 项问诊、相似报障、回访、摘要和重开回归测试；全部测试共 91 项通过。
- 新增 `releases/` 历史版本目录规范；版本快照不包含真实密钥、数据库、日志和构建产物。

## V1.6.0 (2026-07-15)

**内置 SQLite 工单数据库生产化**

- 工单数据库路径可通过 `ticket.database_path` 配置，相对路径以项目根目录为基准。
- 启动时执行 `PRAGMA integrity_check`，检测到损坏会拒绝继续启动。
- 新增 `schema_meta` 表和 Schema 版本，当前版本为 `1`。
- 所有数据库连接统一启用外键和可配置的 `busy_timeout`。
- 使用 SQLite 在线备份 API 每日生成一致性备份，默认保存于 `data/backups/`。
- 支持配置备份保留天数，启动时自动清理过期备份。
- 新增数据库完整性、Schema、备份可恢复性和过期清理测试。

## V1.5.1 (2026-07-15)

**安全与会话隔离修复（功能审查问题 2–7）**

| 类型 | 文件 | 改动 | 说明 |
|------|------|------|------|
| 🔴安全 | `handlers/chat_handler.py` | 附件文件名净化 + UUID 落盘名 | 阻止 `../`、绝对路径和 Windows 非法字符造成路径穿越或覆盖 |
| 🔴修复 | `handlers/chat_handler.py` | 下载前检查钉钉 OpenAPI 客户端 | 未配置附件/语音能力时返回明确提示，不再空对象调用 |
| 🛡️隔离 | `handlers/chat_handler.py` | AI 手动引擎改为会话级状态 | 群聊或私聊切换不再影响其他会话，30 分钟无交互自动恢复 |
| 🔴修复 | `handlers/chat_handler.py` | 支持 CodeBuddy-only 普通对话 | 未配置 DeepSeek 时自动选择 CodeBuddy，不再落入默认回复 |
| 🛡️加固 | `modules/ticket/ticket_manager.py` | 工单状态转换表及存在性验证 | 禁止已关闭工单重开/分配，避免不存在工单产生孤立日志 |
| 🛡️加固 | `handlers/chat_handler.py` | 附件状态加锁、定时清理 | 暂存附件每 5 分钟扫描；替换、关联、过期后删除本地临时文件 |
| ✅测试 | `tests/test_regressions.py` | 新增 7 项回归测试 | 覆盖附件路径、会话隔离、TTL、非法流转和孤立日志 |
| ✅测试 | `tests/test_ticket.py` | 同步当前工单 API | 修复旧测试引用已删除方法，并兼容 Windows UTF-8 控制台 |

## V1.5.0 (2026-06-09)

**稳定性优先：全局异常兜底 + 语音流程修复 + 线程安全加固**

| 类型 | 文件 | 改动 | 说明 |
|------|------|------|------|
| 🔴修复 | `chat_handler.py` | `process()` 入口加全局 try-except 兜底 | 任何未预期异常不再导致消息处理中断无回复，自动回复友好提示 |
| 🔴修复 | `chat_handler.py` | 语音转文字走完整 `_dispatch_text_message()` | 之前走简化版 `_process_text_message()`，缺少工单指令、反馈检测、引擎切换等全部逻辑 |
| 🔴修复 | `chat_handler.py` | `_judge_priority()` 显式捕获 `DeepSeekAPIError` | 优先级判断 API 错误时精确分类日志，回退 medium |
| 🔴修复 | `chat_handler.py` | 消息调度代码去重 | `process()` 和 `_process_text_message()` 合并为 `_dispatch_text_message()`，消除 100+ 行重复代码 |
| 🛡️加固 | `chat_handler.py` | `_pending_feedback` 加 `threading.Lock` | 反馈状态读写线程安全，防止竞态条件 |
| 🛡️加固 | `chat_handler.py` | `_pending_attachments` 正式初始化 + TTL 清理 | 1 小时过期自动清理暂存附件+磁盘文件，防止内存泄漏 |
| 🛡️加固 | `chat_handler.py` | 语音临时文件转完后自动删除 | `data/voice/` 不再持续膨胀 |
| 📦补全 | `requirements.txt` | 添加 `openai-whisper` 依赖声明 | 语音转文字的可选依赖已标注 |
| 📦更新 | `utils/console.py` | `BOT_VERSION` → V1.5.0 | — |

**重构要点**：
- `_process_text_message()` → `_dispatch_text_message()`，包含完整处理链（专家→触发词→引擎切换→专家意图→反馈→工单→AI对话→默认）
- `process()` 仅保留：消息解析 → 全局兜底 try → 非文本分流 → 文件测试 → 调用 `_dispatch_text_message()`
- 两条消息路径（直接文本 / 语音转文字后）现在走完全相同的处理逻辑

## V1.4.1 (2026-06-08)

**修复：V1.4.0 改动导致的启动崩溃 + 异常穿透**

V1.4.0 中补全的线程安全/异常类改动缺少配套 import 和异常捕获，导致机器人 `__init__` 直接 `NameError` 崩溃，Stream 连接无法建立。

| 类型 | 文件 | 问题 | 修复 |
|------|------|------|------|
| 🔴修复 | `handlers/chat_handler.py` | 加了 `threading.Lock()` 但缺少 `import threading`，`__init__` 直接 `NameError` 崩溃，**机器人无法启动** | 顶部添加 `import threading` |
| 🔴修复 | `handlers/chat_handler.py` | `deepseek_client.py` 改抛 `DeepSeekAPIError`，但 `llm_client.chat()` 调用处无 `try-except`，异常穿透导致消息处理崩溃 | AI 引擎调度段 CodeBuddy/DeepSeek 调用均加 `try-except DeepSeekAPIError` |
| 修改 | `handlers/chat_handler.py` | import 补充 `from utils.deepseek_client import DeepSeekAPIError` | — |

**崩溃链路**：
```
V1.4.0 加了 self.__expert_lock__ = threading.Lock()
  → 但 chat_handler.py 没有 import threading
  → ChatHandler.__init__() 执行到 threading.Lock() → NameError
  → 整个机器人启动失败 → Stream 连接无法建立 → 钉钉收不到消息
```

## V1.4.0 (2026-06-08)

**6大新功能 + 7个审查修复 + 6个健壮性补全**

| 类型 | 功能/修复 | 说明 |
|------|----------|------|
| ✨新功能 | **工单优先级（AI自动判断）** | AI 根据是否影响业务自动判断优先级（🔴紧急/🟠高/🟡中/⚪低）；关键词快速匹配+LLM精确判断双策略；工单列表按优先级排序 |
| ✨新功能 | **文件附件支持** | 支持图片/PDF/日志文件；自动下载并关联到用户最近的待处理工单；无工单时暂存附件等待文字描述 |
| ✨新功能 | **语音识别（Whisper）** | 钉钉语音消息 → 本地 Whisper ASR 转文字 → 作为普通消息处理；支持 AMR/MP3/WAV 格式 |
| ✨新功能 | **相似工单推荐** | 创建工单时自动搜索标题/描述相似的已解决工单，提示用户参考 |
| ✨新功能 | **逾期自动提醒** | 后台每30分钟检查一次；根据优先级阈值（紧急2h/高8h/中24h/低72h）判定逾期；私聊通知管理员；避免重复提醒 |
| ✨新功能 | **每周统计报告** | 每周五12:00自动生成报告；发送给配置的报告接收人；含新增/解决/逾期数、分类统计、平均解决时长 |
| 🔧修复 | `add_admin_from_dingtalk()` 死锁+连接泄漏 | 锁内仅检查 DB，锁外调用 add_admin()；conn.close() 移入 finally |
| 🔧修复 | `archive_ticket()` 死代码删除 | 从未被调用，已被 `archive_as_experience()` 替代 |
| 🔧修复 | `_ticket_close()` 不通知创建者 | 管理员关闭他人工单时追加通知 |
| 🔧修复 | `msg_logger.record()` 调用方式错误 | 统一改为传 dict + 加 try-except |
| 🔧修复 | `_ticket_list_pending/reminder` 查询效率 | 改用 `list_all_tickets(filter_type="pending")` |
| 🔧修复 | `_notify_admin_new_ticket` 缺 staff_id 警告 | 与 `_notify_admin_feedback` 保持一致 |
| 🔧修复 | `format_feedback_yes` 死代码分支 | 简化为无参数方法；修正 FEEDBACK_PROMPT 文案 |
| 🔧修复 | 优先级 `normal` → `medium` 迁移 | 建表默认值+DB自动迁移 |
| 🛡️健壮性 | `api_tracker` 内存泄漏上限 | `_MAX_CALLS = 1000`，超限自动淘汰最早记录 |
| 🛡️健壮性 | `_seen_convs` 内存泄漏上限 + 加锁 | `_SEEN_CONVS_MAX = 10000`，超限淘汰前一半；加 `__seen_convs_lock__` 保护 |
| 🛡️健壮性 | `expert_states` 线程安全 | 加 `__expert_lock__`，`_set_expert_state`/`_get_expert_state`/`_handle_expert_exit` 均加锁 |
| 🛡️健壮性 | `DeepSeekClient` 异常类 | 新增 `DeepSeekAPIError`，超时/HTTP错误/通用异常均抛出而非返回字符串 |
| 🛡️健壮性 | `get_access_token` 重试+锁 | 双重检查锁 + 2次重试 + 指数退避(1s/2s) |
| 🛡️健壮性 | 新增 `.gitignore` | 标准 Python .gitignore（含 `__pycache__`、日志、本地配置等） |

**新增文件**：
- `utils/speech_to_text.py` — Whisper 语音转文字工具
- `utils/scheduled_tasks.py` — 逾期提醒 + 每周报告定时任务

**新增依赖**：
- `openai-whisper` — 语音识别（pip install openai-whisper）
- `ffmpeg` — AMR 音频转 WAV（系统级依赖，需单独安装）

**新增配置** (`config.yaml`)：
- `weekly_report.recipients` — 每周报告接收人列表

## V1.3.8 (2026-06-08)

**全面代码审查 — 修复7个问题**

| 类型 | 文件 | 问题 | 修复 |
|------|------|------|------|
| 修复🔴 | `modules/ticket/ticket_manager.py` | `add_admin_from_dingtalk()` 在持有 `threading.Lock` 时调用 `self.add_admin()`，后者也获取同一把锁，**造成死锁** | 锁内仅检查 DB，锁外调用 `add_admin()`；同时 `conn.close()` 移入 `finally` |
| 修复🟠 | `modules/ticket/ticket_manager.py` | `archive_ticket()` 从未被调用（已被 `archive_as_experience()` 替代），是死代码 | 删除 |
| 修复🟠 | `handlers/chat_handler.py` | `_ticket_close()` 关闭工单后不通知创建者，而 `_ticket_resolve()` 会通知 | 管理员关闭他人工单时追加 `_notify_user_resolved()` |
| 修复🟠 | `handlers/chat_handler.py` | `_ticket_create()` / `_handle_feedback_no()` 调用 `msg_logger.record()` 传关键字参数而非 dict，且字段名 `time_str` 与方法要求的 `time` 不匹配；`_ticket_create` 无 try-except 会抛 TypeError | 统一改为传 dict + 加 try-except 保护 |
| 修复🟡 | `handlers/chat_handler.py` | `_ticket_list_pending()` / `_ticket_reminder()` 先拉全量再 Python 过滤 | 改用已有的 `list_all_tickets(filter_type="pending")` |
| 修复🟡 | `handlers/chat_handler.py` | `_notify_admin_new_ticket()` 缺少 staff_id 为空时的警告日志 | 补充，与 `_notify_admin_feedback()` 保持一致 |
| 修复🟡 | `modules/ticket/ticket_formatter.py` | `format_feedback_yes()` 的 `ticket_id` 参数永远传 None，是死代码分支 | 简化为无参数方法 |
| 修改 | `utils/console.py` | 版本号 V1.3.8 | |
| 修改 | `main.py` | 版本注释 V1.3.8 | |

## V1.3.7 (2026-06-08)

**修复：工单创建和处理逻辑的多个问题**

| 类型 | 文件 | 问题 | 修复 |
|------|------|------|------|
| 修复🔴 | `handlers/chat_handler.py` | `_handle_feedback_yes()` 中 `pending.get("ticket_id")` 是死代码（V1.3.5 已移除 ticket_id），归档逻辑永远不触发 | 清理死代码，"是"反馈只表示感谢，工单由管理员手动处理 |
| 修复🔴 | `handlers/chat_handler.py` | `RESOLVE` 解决工单指令无管理员权限校验，普通用户可解决任意工单 | dispatch 新增 `if not is_admin` 校验 |
| 修复🟠 | `handlers/chat_handler.py` | `_ticket_resolve()` 中 `parsed.extra` 为消息全文（含编号），resolution 字段存了冗余前缀 | 新增文本提取逻辑：去除编号、动词前缀、分隔符 |
| 修复🟠 | `modules/ticket/ticket_manager.py` | `archive_as_experience()` 把状态设为 `resolved` 而非 `closed`，已归档工单仍出现在"未关闭"统计中 | 改为 `STATUS_CLOSED` + 同步写 `resolved_at` |
| 修复🟡 | `modules/ticket/ticket_parser.py` | 优先级注释编号断层（"4.5"/"4.6"），代码与注释不一致 | 统一为连续编号 1~11 |
| 修改 | `utils/console.py` | 版本号 V1.3.7 | |
| 修改 | `main.py` | 版本注释 V1.3.7 | |

## V1.3.6 (2026-06-08)

**新增：管理员全量工单查询入口**

管理员可通过自然语言指令查询所有工单，支持三种筛选模式：

| 指令示例 | 显示内容 |
|----------|----------|
| 「**所有工单**」/ 「**全部工单**」 | 所有工单，按状态分组 |
| 「**已处理工单**」/ 「**历史工单**」/ 「**已完成工单**」 | 状态为 resolved / closed 的工单 |
| 「**未处理工单**」（新触发词） | 状态为 open / assigned / in_progress 的工单 |

此前「所有工单」被归类到"待处理队列"触发词，现独立为 `LIST_ALL` 意图（仅管理员可用），原"待处理"队列保持不变。

| 类型 | 文件 | 内容 |
|------|------|------|
| 修改 | `modules/ticket/ticket_parser.py` | 新增 `LIST_ALL` intent；新增 `LIST_ALL_KEYWORDS`；parse() 新增第4优先级解析块；将"所有工单"从 `LIST_PENDING_KEYWORDS` 移出 |
| 新增 | `modules/ticket/ticket_manager.py` | `list_all_tickets(filter_type, limit)` — 支持 all/pending/done 三种过滤 |
| 新增 | `modules/ticket/ticket_formatter.py` | `format_all_tickets_admin(tickets, filter_type)` — 按状态分组展示，含快捷指令提示 |
| 修改 | `modules/ticket/ticket_formatter.py` | `format_help()` — 新增「所有工单」/「已处理工单」/「历史工单」指令说明 |
| 新增 | `handlers/chat_handler.py` | `_ticket_list_all()` 方法；dispatch 新增 `LIST_ALL` 分支（仅管理员） |
| 修改 | `utils/console.py` | 版本号 V1.3.6 |
| 修改 | `main.py` | 版本注释 V1.3.6 |

## V1.3.5 (2026-06-05)

**修复：每次反馈"否"创建独立新工单，不再复用同会话活跃工单**

此前同一会话内多次反馈"否"会复用已有活跃工单，导致不同问题指向同一工单编号。
V1.3.5 改为：每次反馈"否"都独立创建新工单，每个问题有独立编号和记录。

| 类型 | 文件 | 内容 |
|------|------|------|
| 修改 | `handlers/chat_handler.py` | `_handle_feedback_no()` — 移除工单复用逻辑，始终新建工单 |
| 修改 | `handlers/chat_handler.py` | `_set_pending_feedback()` — 移除 `ticket_id` 参数，不再绑定活跃工单 |
| 修改 | `handlers/chat_handler.py` | AI 回答后设置 pending 时不再查询活跃工单 |
| 修改 | `utils/console.py` | 版本号 V1.3.5 |
| 修改 | `main.py` | 版本注释 V1.3.5 |

**工单编码结构**：`TKT-YYYYMMDD-NNN`（如 TKT-20260605-001），每日递增。

## V1.3.4 (2026-06-05)

**优化：管理员创建工单时也收到副本确认通知**

此前管理员自己触发工单创建（反馈不满意/直接创建）时，代码为避免重复通知会跳过自己，
导致只有一个管理员的情况下私聊通知日志一条都没有。

V1.3.4 改为：创建者（若也是管理员）收到轻量级「副本确认」，其他管理员收到完整通知。

| 类型 | 文件 | 内容 |
|------|------|------|
| 新增 | `modules/ticket/ticket_formatter.py` | `format_admin_self_ticket_confirmation()` — 管理员自建工单副本确认模板 |
| 修改 | `handlers/chat_handler.py` | `_notify_admin_new_ticket()` — 不再跳过创建者，改为发送副本确认 |
| 修改 | `handlers/chat_handler.py` | `_notify_admin_feedback()` — 同上，反馈者收到副本确认 |
| 修改 | `utils/console.py` | 版本号 V1.3.4 |
| 修改 | `main.py` | 版本注释 V1.3.4 |

**副本确认 vs 完整通知**：
- 📋 **副本确认**：轻量级，标题「📋 工单副本」，提示"您创建的工单已记录"
- 🆕 **完整通知**：完整工单详情，标题「🆕 新工单」，提示"请尽快处理"

## V1.3.3 (2026-06-05)

**修复：staffId.notExisted —— 自动解析 + 手动绑定双保险**

钉钉 Stream SDK 默认不返回 `sender_staff_id`（需要应用权限），导致私聊通知始终失败。
V1.3.3 新增 API 主动解析 + 手动绑定命令双保险。

| 类型 | 文件 | 内容 |
|------|------|------|
| 新增 | `utils/dingtalk_api.py` | `resolve_staff_id_by_nick()` — 通过通讯录 API 按昵称查找 staffId |
| 新增 | `utils/dingtalk_api.py` | `resolve_staff_id_by_open_id()` — 通过 unionId 接口解析 staffId |
| 新增 | `handlers/chat_handler.py` | `_resolve_staff_id()` — 解析优先级：消息自带 → unionId 查询 → 昵称查询 |
| 新增 | `handlers/chat_handler.py` | `_ticket_bind_staff_id()` — 处理「绑定工号」命令 |
| 新增 | `modules/ticket/ticket_parser.py` | `TicketIntent.BIND_STAFF_ID` 意图 + 关键词 |
| 修改 | `handlers/chat_handler.py` | `_is_ticket_admin()` — 绑定管理员时自动调用 `_resolve_staff_id()` 解析 staffId |
| 修改 | `handlers/chat_handler.py` | `_extract_message_info()` — 增加调试日志 dump 原始消息字段 |
| 修改 | `modules/ticket/ticket_formatter.py` | `format_help()` — 帮助中新增「绑定工号」命令说明 |
| 修改 | `utils/console.py` | 版本号 V1.3.3 |
| 修改 | `main.py` | 版本注释 V1.3.3 |

**「绑定工号」用法**：
```
绑定工号 01234567
设置ID 01234567
```

**解析优先级**：
1. 消息中 `sender_staff_id` 字段（需钉钉开放平台开通权限）
2. 通过 `sender_id` 调用 `/topapi/user/getbyunionid` 查询（需通讯录权限）
3. 通过用户昵称调用 `/topapi/v2/user/list` 在通讯录中匹配
4. 以上均失败时，使用「绑定工号」手动设置

**注意**：步骤 2/3 需要应用具备「通讯录部门用户读权限」，
若权限未开通，请直接在钉钉中发送「绑定工号 xxxxxxxx」手动设置。

## V1.3.2 (2026-06-05)

**修复：私聊通知 staffId.notExisted — 改用企业 userId 发送**

| 类型 | 文件 | 内容 |
|------|------|------|
| 修复 | `handlers/chat_handler.py` | `_notify_admin_new_ticket()` — 私聊通知改用 `staff_id`（企业userId），不再用 `user_id`（openId） |
| 修复 | `handlers/chat_handler.py` | `_notify_admin_feedback()` — 同上，私聊通知改用 `staff_id` |
| 修复 | `handlers/chat_handler.py` | `_notify_user_resolved()` — 工单解决通知改用 `creator_staff_id` |
| 修复 | `handlers/chat_handler.py` | `_is_ticket_admin()` — 新增 `staff_id` 参数，管理员绑定时同步写入 staff_id |
| 修复 | `handlers/chat_handler.py` | `_ticket_create()` — 创建工单时写入 `creator_staff_id` |
| 修复 | `handlers/chat_handler.py` | `_handle_feedback_no()` — 反馈升级创建工单时也写入 `creator_staff_id` |
| 修改 | `modules/ticket/ticket_manager.py` | admins 表新增 `staff_id` 字段 |
| 修改 | `modules/ticket/ticket_manager.py` | tickets 表新增 `creator_staff_id` 字段 |
| 修改 | `modules/ticket/ticket_manager.py` | `_migrate_columns()` — 支持两张表的新字段自动迁移 |
| 修改 | `modules/ticket/ticket_manager.py` | `add_admin()` — 新增 `staff_id` 参数 |
| 修改 | `modules/ticket/ticket_manager.py` | `add_admin_from_dingtalk()` — 新增 `staff_id` 参数 |
| 修改 | `modules/ticket/ticket_manager.py` | `create_ticket()` — 新增 `creator_staff_id` 参数 |
| 修改 | `modules/ticket/ticket_manager.py` | `_row_to_dict()` — 新增 `creator_staff_id` 字段映射 |

**根因**：`oToMessages/batchSend` API 的 `userIds` 参数要求企业内 **staffId**（如 `01234567`），但代码存的是 Stream SDK 的 `sender_id`（openId/unionId 格式），导致钉钉返回 `staffId.notExisted` 错误。`_extract_message_info()` 已提取 `sender_staff_id`，但私聊通知时从未使用。

**关键逻辑**：内部匹配（is_admin、creator_id 比较）仍用 `sender_id`（openId），发送 API 请求时用 `staff_id`（企业userId）。两者解耦，互不影响。

## V1.3.1 (2026-06-05)

**修复：群聊通知 400 Bad Request + 错误日志增强**

| 类型 | 文件 | 内容 |
|------|------|------|
| 修复 | `handlers/chat_handler.py` | `_notify_admin_feedback()` / `_notify_admin_new_ticket()` — 仅群聊时调用 `send_markdown_to_chat`，单聊跳过 |
| 修复 | `utils/dingtalk_api.py` | `send_markdown_to_chat()` — 新增响应体错误日志（HTTP 非 200 时打印 `resp.text`） |
| 修复 | `utils/dingtalk_api.py` | `send_markdown_to_chat()` / `send_markdown_to_user()` — 新增内容截断（title 50 字符 / text 16000 字符） |

**根因**：用户在单聊中创建工单/反馈"否"时，代码无差别调用 `groupMessages/send` API，但该 API 仅支持群聊 conversationId，单聊的 conv_id 传入导致钉钉返回 400。

## V1.3.0 (2026-06-05)

**工单系统增强：归档经验 + 无效标记 + 权限分离 + 主动提醒**

| 类型 | 文件 | 内容 |
|------|------|------|
| 新增 | `modules/ticket/ticket_manager.py` | 新增 `archive_as_experience()` — 归档为经验（is_experience=1）|
| 新增 | `modules/ticket/ticket_manager.py` | 新增 `mark_as_invalid()` — 标记为无效工单（is_invalid=1）|
| 新增 | `modules/ticket/ticket_manager.py` | 新增 `_migrate_columns()` — 自动迁移旧 DB 新增字段 |
| 新增 | `modules/ticket/ticket_manager.py` | DB 新增字段 `resolver_id`、`resolver_nick`、`is_invalid`、`is_experience` |
| 修改 | `modules/ticket/ticket_manager.py` | `resolve_ticket()` / `close_ticket()` — UPDATE 补充 resolver_id/resolver_nick |
| 修改 | `modules/ticket/ticket_manager.py` | `_row_to_dict()` — 重写为兼容旧 DB 的安全索引版本 |
| 新增 | `modules/ticket/ticket_parser.py` | 新增意图 `ARCHIVE`、`INVALID`、`REMINDER` + 对应关键词 |
| 新增 | `modules/ticket/ticket_formatter.py` | 新增 `format_archive_prompt()` — 解决后追问管理员是否归档 |
| 新增 | `modules/ticket/ticket_formatter.py` | 新增 `format_archived_confirmation()` / `format_invalid_confirmation()` |
| 新增 | `modules/ticket/ticket_formatter.py` | 新增 `format_reminder()` — 主动工单提醒格式 |
| 修改 | `modules/ticket/ticket_formatter.py` | `format_ticket_detail()` — 展示处理人（与创建人分开）+ 归档/无效标记 |
| 修改 | `modules/ticket/ticket_formatter.py` | `format_help()` — 新增归档/无效/提醒指令说明 |
| 新增 | `handlers/chat_handler.py` | 新增 `_ticket_archive()` — 归档为经验 |
| 新增 | `handlers/chat_handler.py` | 新增 `_ticket_invalid()` — 标记无效工单 |
| 新增 | `handlers/chat_handler.py` | 新增 `_ticket_reminder()` — 主动工单提醒 |
| 修改 | `handlers/chat_handler.py` | `_ticket_view()` — 权限检查：普通用户只能查看自己的工单，管理员可查看所有 |
| 修改 | `handlers/chat_handler.py` | `_ticket_view()` — 管理员查看已解决未归档工单时自动追问是否归档 |
| 修改 | `handlers/chat_handler.py` | `_ticket_resolve()` — 解决后自动追问是否归档为经验 |
| 修改 | `main.py` / `utils/console.py` | 版本号 V1.2.5 → V1.3.0 |

**核心变更**：
1. **工单创建人与处理人分离** — 新增 `resolver_id` / `resolver_nick` 字段，解决/关闭时自动记录处理人
2. **归档经验机制** — 管理员解决/查看工单后追问是否归档为经验，归档后 `is_experience=1`
3. **无效工单标记** — 不归档的工单标记为 `is_invalid=1`，不进入经验库
4. **权限分离** — 普通用户只能查看自己创建的工单，管理员可查看所有
5. **主动工单提醒** — 管理员回复「工单提醒」查看待处理工单列表

## V1.2.5 (2026-06-05)

**修复：工单通知闭环补全 — 管理员创建通知 + 用户解决反馈**

| 类型 | 文件 | 内容 |
|------|------|------|
| 新增 | `modules/ticket/ticket_formatter.py` | 新增 `format_admin_new_ticket_notification()` — 工单创建时发送给管理员的通知格式；新增 `format_user_resolved_notification()` — 工单解决后发送给创建者的通知格式 |
| 新增 | `handlers/chat_handler.py` | 新增 `_notify_admin_new_ticket()` — 工单创建后群聊+私聊通知管理员（排除工单创建者本人） |
| 新增 | `handlers/chat_handler.py` | 新增 `_notify_user_resolved()` — 工单解决后私聊通知工单创建者（包含解决方案，排除自己解决自己工单的情况） |
| 修改 | `handlers/chat_handler.py` | `_ticket_create()` 工单创建后追加调用 `_notify_admin_new_ticket()` |
| 修改 | `handlers/chat_handler.py` | `_ticket_resolve()` 工单解决后追加调用 `_notify_user_resolved()` |
| 修改 | `modules/ticket/ticket_formatter.py` | `format_ticket_created()` 新增"已通知管理员"和"管理员会私聊通知你"提示 |
| 修改 | `modules/ticket/ticket_formatter.py` | `format_help()` 新增管理员操作指引：`解决 TKT-xxx 方案` → 用户自动收到通知 |
| 修改 | `modules/ticket/ticket_formatter.py` | `format_admin_feedback_notification()` 增加"待处理"命令提示 |
| 修改 | `main.py` | 版本号 V1.2.4 → V1.2.5 |
| 修改 | `utils/console.py` | 版本号 V1.2.4 → V1.2.5 |

**修复前问题**：
1. 用户直接创建工单（报修/提交工单）时，管理员**收不到通知**（只有反馈"否"才通知）
2. 管理员解决工单后，工单创建者**收不到解决反馈**

**修复后闭环**：
```
用户提交工单 → 群聊+私聊通知管理员 → 管理员回复「解决 TKT-xxx 方案」
  → 私聊通知工单创建者（含解决方案）→ 用户确认 → 完整闭环
```

## V1.2.4 (2026-06-03)

**新增：AI 回答反馈闭环（是/否 → 归档/升级工单）**

| 类型 | 文件 | 内容 |
|------|------|------|
| 修改 | `handlers/chat_handler.py` | 新增 `_pending_feedback` 状态字典（30分钟TTL）；`_handle_ai_chat` 回答后自动追加反馈提示；`process()` 新增 step 2.8 反馈检测（检测"是/否"）；`_handle_feedback_yes`→自动归档工单；`_handle_feedback_no`→创建工单→设置处理中→通知管理员 |
| 修改 | `modules/ticket/ticket_parser.py` | 新增 `FEEDBACK_YES`/`FEEDBACK_NO` 意图 + `detect_feedback()` 方法；添加中英文"是/否"系列关键词 |
| 修改 | `modules/ticket/ticket_manager.py` | 新增 `set_in_progress()`/`archive_ticket()`/`find_active_ticket_by_conv()` |
| 修改 | `modules/ticket/ticket_formatter.py` | 新增 `FEEDBACK_PROMPT` 常量；新增 `format_feedback_yes()`/`format_feedback_no()`/`format_admin_feedback_notification()` |
| 修改 | `utils/dingtalk_api.py` | 新增 `send_markdown_to_user()` 私聊通知方法 |
| 修改 | `utils/console.py` | 帮助文本更新，新增反馈闭环说明 |
| 修改 | `main.py` | 版本号 V1.2.2 → V1.2.4 |

**反馈闭环流程**：
```
AI 回答 → 追加「这个回答是否为您提供了帮助？是/否」
  ├─ 用户回复「是」→ 自动归档工单（resolved）
  └─ 用户回复「否」→ 创建工单 → 设置处理中 → 群聊+私聊通知管理员
       └─ 管理员回复「解决 TKT-xxx」→ 标记已解决
```

## V1.2.3 (2026-06-03)

**重构：工单分类体系 + 移除分配 + 每日摘要**

| 类型 | 文件 | 内容 |
|------|------|------|
| 修改 | `modules/ticket/ticket_manager.py` | 重定义 CATEGORIES（设备问题/业务系统问题/CRM问题/软件问题/其他）；新增知识库分类映射 + `classify_by_kb()` 智能分类；新增 `get_daily_summary()` |
| 修改 | `modules/ticket/ticket_parser.py` | 移除 `ASSIGN`/`START` 意图；新增 `DAILY` 每日摘要意图；`STATS` 改为全员可用 |
| 修改 | `modules/ticket/ticket_formatter.py` | 移除 `format_ticket_assigned()`；新增 `format_daily_summary()`；更新 `format_help()`/`format_pending_queue()` |
| 修改 | `handlers/chat_handler.py` | 移除 `_ticket_assign()`/`_ticket_start()`；`_ticket_create()` 改为基于 IMA 知识库搜索结果智能分类（KB 命中分类 → 关键词兜底）；新增 `_ticket_daily()` |
| 修改 | `utils/console.py` | 控制台帮助更新（移除分配/处理命令，加入日报命令） |

**分类逻辑**：
1. 搜索 IMA 知识库 → 按 `source_kb_id` 映射分类（精准）
2. KB 名称关键词匹配（兜底）
3. 内容关键词匹配（最终兜底）

**新增指令**：`日报` / `每日工单` / `工单日报` / `今日摘要`

## V1.2.2 (2026-06-03)

**优化：工单管理员昵称绑定 + 自动 user_id 写入**

| 类型 | 文件 | 内容 |
|------|------|------|
| 修改 | `handlers/chat_handler.py` | `_is_ticket_admin()` 升级：支持昵称匹配(config `user_nick`) → 首次匹配自动将 user_id 写入 DB → 后续走精确 ID 匹配；调用处同步传 `user_nick` |
| 修改 | `config.yaml` | `ticket.admins[0].user_nick` 可设为指定管理员昵称，`user_id` 留空（首次触发自动绑定） |

**管理员匹配优先级**：
1. `user_id` 精确匹配（DB 已有记录，最快）
2. `config.yaml` 昵称匹配（`user_nick: "管理员昵称"`）→ 首次自动写入 DB
3. 管理员表为空 → 首个交互用户自动成为管理员

**效果**：重启机器人后，指定管理员发送任意消息即绑定为永久管理员，无需手动查 user_id。

---
## V1.2.1 (2026-06-03)

**新增：IT 工单系统**

| 类型 | 文件 | 内容 |
|------|------|------|
| 新增 | `modules/ticket/` | 完整工单模块目录：`TicketManager`(SQLite CRUD)、`TicketParser`(意图识别)、`TicketFormatter`(Markdown输出) |
| 新增 | `data/tickets.db` | 本地 SQLite 数据库：工单表(tickets)、操作日志表(ticket_log)、管理员表(admins) |
| 修改 | `handlers/chat_handler.py` | 新增工单处理步骤(step 2.85)、10个工单操作方法、自动管理员初始化 |
| 修改 | `utils/console.py` | 新增控制台命令：`ticket`/`工单`(帮助)、`ticket stats`/`工单统计`、`ticket list`/`工单列表` |
| 修改 | `config.yaml` | 新增 `ticket` 配置节（启用开关 + 管理员列表） |

**工单生命周期**：提交(category auto-classify) → 分配 → 处理中 → 解决/关闭

**用户指令 (群聊/单聊)**：

| 指令 | 说明 |
|------|------|
| `报修` / `提交工单` / 描述故障 | 创建工单（自动分类：ERP/网络/硬件/软件/账户） |
| `我的工单` | 查看我提交的工单列表 |
| `工单 TKT-xxx` / `#TKT-xxx` | 查看工单详情 + 操作日志 |
| `解决了` / `关闭工单` | 关闭工单（智能匹配最近一条） |
| `待处理` / `工单队列` | 管理员：查看待处理队列 |
| `分配 TKT-xxx 给 张三` | 管理员：分配工单 |
| `开始处理 TKT-xxx` | 管理员：接手处理 |
| `工单统计` / `今日工单` / `周统计` | 管理员：统计数据 |

**控制台命令**：
| `ticket` / `工单` | 工单系统帮助 |
| `ticket stats` / `工单统计` | 查看工单统计 |
| `ticket list` / `工单列表` | 列出最近工单 |

**设计原则**：
- 数据库使用 SQLite，零外部依赖，停服复制即迁移
- 管理员表为空时首个用户自动成为管理员
- 工单编号格式：`TKT-YYYYMMDD-NNN`

## V1.2.0 (2026-06-03)

**新增：消息日志分类导出功能**

| 类型 | 文件 | 内容 |
|------|------|------|
| 新增 | `utils/message_logger.py` | 新建 `MessageLogger` 消息日志记录器：按天自动创建 `logs/messages/YYYY-MM-DD.jsonl`，每条消息记录完整结构化字段 |
| 新增 | `utils/message_logger.py` | `export_by_sender()` — 按提问人分类导出为独立 `.txt` 文件 |
| 新增 | `utils/message_logger.py` | `export_by_expert()` — 按专家模式分类导出为独立 `.txt` 文件 |
| 新增 | `utils/message_logger.py` | `export_summary()` — 全量导出（两个维度都导出） |
| 新增 | `utils/message_logger.py` | `list_available_dates()` — 列出有日志的所有日期 |
| 修改 | `handlers/chat_handler.py` | `_handle_ai_chat()` 回复后追加结构化日志记录 |
| 修改 | `utils/console.py` | 新增控制台命令 `export` 系列 + 帮助文本更新 |

**每条日志记录内容**：时间 / 消息ID / 提问人(昵称+ID) / 对话类型 / 群聊名称 / 用户消息 / 专家模式 / 知识库结果数 / AI引擎 / 回答长度 / 回答摘要

**控制台命令**：

| 命令 | 效果 |
|------|------|
| `export user` / `导出用户` | 按提问人分类导出今日日志 |
| `export expert` / `导出专家` | 按专家模式分类导出今日日志 |
| `export all` / `导出全部` | 全量导出（两个维度） |
| `export list` / `导出列表` | 列出可导出的日志日期 |
| `export user 2026-06-03` | 导出指定日期的日志 |

**导出目录**：`logs/messages/exports/YYYY-MM-DD/by-sender/` 和 `by-expert/`

---

## V1.1.9 (2026-06-03)

**新增：专家模式主动退出 / 切换 / 智能提醒**

| 类型 | 文件 | 内容 |
|------|------|------|
| 新增 | `handlers/chat_handler.py` | 新增 `_detect_expert_intent()` 专家意图检测：识别退出/切换/列表/切换失败四种意图 |
| 新增 | `handlers/chat_handler.py` | 新增 `_handle_expert_exit()` 退出处理：清除专家状态 + 通知用户回到普通模式 |
| 新增 | `handlers/chat_handler.py` | 新增 `_handle_expert_switch()` 切换处理：匹配目标专家触发词 + 更新会话状态 |
| 新增 | `handlers/chat_handler.py` | 新增 `_handle_expert_list()` 列表展示：显示当前专家和所有可用专家 + 操作指引 |
| 新增 | `handlers/chat_handler.py` | 新增 `_handle_switch_failed()` 切换失败提示：未匹配到专家时列出可选专家列表 |
| 修改 | `handlers/chat_handler.py` | `process()` 新增步骤 2.75：在 AI 对话前检测专家模式意图（退出/切换/列表优先于普通消息处理） |
| 修改 | `handlers/chat_handler.py` | `_handle_ai_chat()` 答案生成：专家模式 + 知识库无结果时，追加切换/退出提醒 |
| 修改 | `handlers/chat_handler.py` | `_send_welcome()` 新手引导：增加退出/切换专家操作说明 |

**支持的专家模式操作**：

| 用户输入 | 效果 |
|----------|------|
| 「退出专家模式」「关闭专家」「不需要专家了」「我自己来」 | 退出专家模式，回到普通模式 |
| 「切换运维专家」「换成业务专家」 | 切换到指定专家 |
| 「当前专家」「有哪些专家」「专家列表」 | 显示当前专家和全部可选专家 |
| 发送不相关问题时知识库无结果 | 回答末尾自动追加切换/退出提示 |

---

## V1.1.8 (2026-06-03)

**优化：RAG 回答精度提升（五项改进）**

| 类型 | 文件 | 内容 |
|------|------|------|
| 新增 | `handlers/knowledge_handler.py` | 新增 `DOMAIN_TERMS` IT运维领域词表（101个复合词），切词时整词优先匹配，防止"料号修改"被切成无意义碎片 |
| 修改 | `handlers/knowledge_handler.py` | `extract_keywords()` 重构：优先整词匹配领域词表 → 英文词 → 剩余中文滑动窗口切词，命中率显著提升 |
| 修改 | `handlers/knowledge_handler.py` | 搜索结果摘要截断长度 150 → 300 字，防止操作步骤被截断 |
| 新增 | `handlers/chat_handler.py` | AI 对话前质量过滤：过滤摘要内容 < 30 字的低质量结果，降低噪音对 AI 的干扰 |
| 修改 | `handlers/chat_handler.py` | 专家模式 `max_results` 自动提升至 8（原5），覆盖更多相关文档 |
| 修改 | `handlers/chat_handler.py` | `_build_system_prompt()` 有知识库结果时增加格式约束：操作步骤必须用编号列表、回答末尾标注来源、禁止补充知识库未提及信息、字数上限 500→800 字 |

**效果预期**：
- 搜索命中率提升（整词匹配领域词汇）
- 回答格式一致（步骤必须编号）
- 来源可追溯（回答末尾必须标注文档名）
- 降低 AI 幻觉（禁止补充知识库未提及信息）

---

## V1.1.7 (2026-06-02)

**修复：CodeBuddy 专家模式返回原始 JSON 文本**

| 类型 | 文件 | 内容 |
|------|------|------|
| 新增 | `utils/codebuddy_client.py` | 新增 `_extract_content()` 通用内容提取器，支持 dict/str/list/裸JSON 多种格式 |
| 修改 | `utils/codebuddy_client.py` | `_parse_sse_line()` 新增裸 JSON（无 SSE 前缀）处理 + CodeBuddy 直接响应格式 `{"version":"1.0","status":"completed","content":{...}}` |
| 修改 | `utils/codebuddy_client.py` | `_is_completion_line()` 强化：支持检测顶层 `status` 字段（不依赖 `data.status`） |
| 修改 | `utils/codebuddy_client.py` | 轮询降级路径的 `result` 提取改用 `_extract_content()` 安全解析，避免盲取原始 JSON |
| 新增 | `handlers/chat_handler.py` | 新增 `_sanitize_answer()` 最后防线：检测并清洗未解析的 JSON 响应再发送到钉钉 |
| 优化 | `utils/codebuddy_client.py` | `_create_run()` 调换顺序：Generic 格式优先（稳定可用），OpenAPI 仅作 fallback |
| 新增 | `utils/codebuddy_client.py` | 新增 `_fix_mojibake()` UTF-8 双重编码修复（如 `é£` → `风`） |
| 修改 | `utils/codebuddy_client.py` | SSE stream 读取强制 `resp.encoding = "utf-8"`，防止 charset 头错误 |

**问题链路**：
```
CodeBuddy 返回 {"version":"1.0","status":"completed","content":{"markdown":"料号修改步骤..."}}
  → _parse_sse_line 不认识此格式 → 空收集
  → _is_completion_line 检测不到完成标记
  → SSE 流无数据 → 回退轮询降级
  → data.get("result") 拿到一整坨 JSON 字符串 → 直接 reply_text
  → 钉钉用户看到原始 JSON
```

---

## V1.1.6 (2026-06-02)

**修复：CodeBuddy 429 限流根因修复 — 配置自定义 DeepSeek 模型绕过云端 API**

| 类型 | 文件 | 内容 |
|------|------|------|
| 新建 | `~/.codebuddy/models.json` | 配置自定义 DeepSeek 模型（id=deepseek-chat, url=https://api.deepseek.com/v1/chat/completions），使用自己的 DeepSeek API Key |
| 新增 | `utils/codebuddy_client.py` | 新增 `_ensure_custom_model()` 方法，检查当前模型是否为 `custom-local:deepseek-chat`，不是则自动切换 |
| 修改 | `utils/codebuddy_client.py` | `_ensure_daemon()` 成功启动 daemon 后自动调用 `_ensure_custom_model()` |
| 修改 | `main.py` | CodeBuddy HTTP 服务启动后自动通过 Settings API 设置默认模型为 `custom-local:deepseek-chat` |

**根因**：CodeBuddy `models.json` 为空 → 默认模型走 `www.codebuddy.ai` 云端 API → 429 限流。自定义模型在 CodeBuddy 内部 ID 为 `custom-local:deepseek-chat`（有前缀），需使用完整 ID 才能正确匹配。

**验证**：创建测试 run `custom-local:deepseek-chat`，模型成功调用 DeepSeek API（ttft=1547ms, 总耗时 1937ms, output=23 字符）。

---

## V1.1.5 (2026-06-02)

**修复：CodeBuddy 429 限流导致超时无响应 + 缩短超时快速 fallback DeepSeek**

| 类型 | 文件 | 内容 |
|------|------|------|
| 修改 | `utils/codebuddy_client.py` | 总超时 DEFAULT_TIMEOUT 120→30 秒，stream 超时 30→10 秒 |
| 新增 | `utils/codebuddy_client.py` | 新增 MAX_ACTIVE_WAIT=20 秒，run 处于 active 超过 20s 主动放弃（可能模型 429 限流或 daemon 异常） |
| 修改 | `utils/codebuddy_client.py` | `chat()` 全部异常路径（超时/429/daemon未启动/创建失败）统一返回 None，由 `chat_handler.py` 自动 fallback 到 DeepSeek |
| 修改 | `utils/codebuddy_client.py` | 去掉 daemon 重试逻辑（active 超时检测已覆盖此场景） |
| 修改 | `config.yaml` | CodeBuddy timeout 90→30 秒（与 DEFAULT_TIMEOUT 一致） |
| 配置 | CodeBuddy Settings API | 通过 `/api/v1/settings/model` 设置默认模型为 `deepseek-v3-2-volc`（内置模型，减少 429 概率） |

**根因**：CodeBuddy `models.json` 为空，默认走 `default-model` → `www.codebuddy.ai` 云端 API → 429 限流 → run 创建成功但永远 active → 机器人等 120 秒超时才 fallback DeepSeek，用户等待时间过长。

**彻底解决**：在 CodeBuddy UI（设置→模型）中配置自定义 DeepSeek 模型（Base URL: `https://api.deepseek.com/v1`，使用自己的 API Key），绕过 CodeBuddy 云端限流。

---

## V1.1.4 (2026-06-02)

**修复：CodeBuddy daemon 中途挂掉导致 Stream 无数据**

| 类型 | 文件 | 内容 |
|------|------|------|
| 新增 | `utils/codebuddy_client.py` | 新增 `_ensure_daemon()` 方法，每次 `chat()` 前确保 Agent Worker 存活；daemon 未运行自动拉起 |
| 修改 | `utils/codebuddy_client.py` | `chat()` 新增 daemon 前置检查，无法启动时快速失败返回错误提示（不再白等 120s） |
| 修改 | `utils/codebuddy_client.py` | 轮询循环增强：每 10 秒输出进度日志；run 超过 15 秒仍为 active 时自动重试拉起 daemon；completed 时额外尝试从 traces 字段提取结果 |
| 重构 | `utils/codebuddy_client.py` | `test_connection()` 改为调用 `_ensure_daemon()`，逻辑复用 |

**根因**：`test_connection()` 只在启动时执行一次，若 daemon 中途异常退出，后续所有 run 均创建成功但无 worker 处理，SSE stream 永远无数据，最终 120 秒超时返回空结果。

---

## V1.1.3 (2026-06-01)

**修复：CodeBuddy API 格式 + Agent Worker 自动启动**

- CodeBuddy `_create_run` 新增格式 fallback：优先 OpenAPI 格式（`text + sender`），400 时回退 Generic 格式（`id + type + text`）
- `_create_run` 兼容 200（旧版）和 202（OpenAPI 规范）两种响应状态码
- main.py 自动启动 CodeBuddy HTTP 服务后，自动调用 `POST /api/v1/daemon/start` 拉起 Agent Worker
- `test_connection` 同时检查 HTTP Gateway 和 Agent Worker (daemon) 状态，daemon 未运行时自动启动
- SSE stream 增强解析：支持 OpenAPI 信封、ACP 风格、OpenAI 兼容、纯文本多种格式
- 新增 stream 读取超时后自动切换到状态轮询模式的降级策略
- 修复 fallback 路径文件后缀：Linux shell 脚本改为 `.cmd`（Windows 可执行）

---

## V1.1.2 (2026-06-01)

**修复：CodeBuddy CLI 自动启动失败**

- `where codebuddy` 在某些终端环境下找不到 managed Node 目录中的 CLI
- 新增 fallback 路径检测：依次尝试 managed Node 各版本目录和 npm 全局目录
- 涉及文件：`main.py`

---

## V1.1.1 (2026-06-01)

**功能：AI 引擎自动调度 + 后台 UI 增强**

- 专家模式自动走 CodeBuddy 优先，失败降级 DeepSeek；普通对话走 DeepSeek 优先
- DeepSeek 回复去掉「与知识库无关内容」前缀提示
- 后台 UI 仪表盘新增「AI 引擎状态」独立区域（双引擎状态 + 调度规则 + 最近回复引擎）
- 启动横幅末尾追加完整操作指令列表（群聊 7 条 + 控制台 5 条）
- 修复 console.py 中 `lines.append` 缩进错误导致的语法错误

## V1.1.0 (2026-06-01)

**功能：CodeBuddy Code HTTP API 集成 + 自动启动**

- 新增 `utils/codebuddy_client.py`，封装 Runs API + SSE 流式读取
- chat_handler 支持「切换AI引擎」「CodeBuddy回复」群聊指令
- main.py 自动启动 CodeBuddy HTTP 服务（端口检测 → CLI 查找 → subprocess 启动 → 退出清理）
- config.yaml 新增 codebuddy 配置段

## V1.0.7-hotfix (2026-06-01)

**修复：Windows GBK 编码崩溃**

- Windows GBK 编码无法输出 emoji（`\U0001f98a`），导致 `UnicodeEncodeError` 崩溃
- main.py 使用 `io.TextIOWrapper` 强制 UTF-8 编码输出
