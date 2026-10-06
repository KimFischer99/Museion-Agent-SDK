# Muse 附件审计与代码复用清单

核对日期：2026-10-06  
输入：用户上传的 `muse-sdk.zip`  
SHA-256：`088277b0a712fec0e2bcecb61c25e5450a277e08cd84579ec963ff2061bbde8c`

## 1. 审计结论

**这是一份有价值的 Muse 运行环境与行为契约素材，不是已经完整实现、可以直接开源的通用 Agent SDK。**

最有价值的可见机制是：定时 heartbeat、独立轻量 hook、固定形状的上下文 brief、默认静默、通知偏好、能力可见性与 Skills 流程。最关键的缺失是：生产决策器、核心 daemon/execd、凭据服务、审批服务、移动端通知和部分 CLI 实现。

本次确认的是附件的具体内容；附件 README 的来源自述不等于已经独立认证其生产来源，也不证明它代表 Muse 的完整版本。没有以同名开源项目的代码、新闻或许可证替代附件证据。

### 1.1 对成品承诺的影响

可以开发**行为与接口兼容的独立主动 SDK**，复用已有宿主模型与工具，不需要复刻 Muse 的云沙盒。

不能诚实地承诺“复制目录就获得 Muse 的全部能力”。Skill 文本有平台工具、设备、账户、文件路径和授权流程依赖。需要 legacy importer、能力提供者、错误映射、策略与实际测试。

## 2. 文件与结构盘点

原压缩包含 1,260 个条目，其中有目录与 macOS 元数据。排除 `__MACOSX`、`.DS_Store` 后，审计了：

| 项目 | 数量 |
|---|---:|
| 有效文件 | 449 |
| 有效文件总字节数 | 4,115,762 |
| Markdown | 245 |
| YAML | 109 |
| JSON | 23 |
| Shell 脚本 | 21 |
| Python 脚本 | 17 |
| `SKILL.md` | 88 |
| JavaScript / TypeScript 源文件 | 0 |

根目录仅见 README、Skill catalog、skills 与 architecture，没有完整 SDK 的根包工程和运行内核。部分子目录的包元数据或对 dist 的引用不能证明实现文件已经包含。

README 提到的 `INDEX-SKILL.md` 在本次快照中没有找到；`SKILL-CATALOG.md` 存在。导入器应依据实际文件扫描，而不是仅依赖 catalog 或 README 的数量陈述。

为避免再分发个性化归档任务，本交接包不附完整 449 路径清单。`audit/selected-source-manifest.json` 提供技术核心文件的 hash、行数和大小；`audit/skills.json` 提供全部 88 个 Skill 的元数据审计，不包含其完整正文。

## 3. Skill 格式、依赖与兼容性

### 3.1 可重复的格式发现

| 检查 | 结果 | 处理 |
|---|---|---|
| 一级 `skills/<dir>/SKILL.md` | 82 个 | 正常发现 |
| artifacts 下嵌套入口 | 6 个 | 必须递归发现并处理共享资源 |
| 含下划线等不满足严格名称规则 | 43 个 | 生成合法 canonical name，保留 aliases |
| name 与直接父目录名不同 | 41 个 | 导出目录与名称同时规范化 |
| 描述超过 1,024 字符 | 1 个：meta-ads，1,247 字符 | 审阅后创建短索引描述，原文不覆盖 |
| metadata 非字符串值 | 全部 88 个均含 includeInPrompt:boolean | 转标准字符串映射，原语义留 sidecar |
| 其他非字符串 metadata | voiceOnly:boolean 1 个；devices:list 1 个 | 不直接作为标准 metadata 值导出 |
| frontmatter 的 license 字段 | 88 个均没有 | 必须检查其他许可来源，不能假定 MIT |

这些项目相互重叠，不能把问题数量相加当成有问题的 Skill 总数。名称转换后的候选 canonical names 在本次 88 条中无碰撞；未来版本仍必须检测碰撞。

官方 Agent Skills 规范要求名称与目录、描述长度及 metadata 值类型符合约束；`allowed-tools` 仍需宿主支持，不能替代本项目权限系统。来源见 SPEC W1。

### 3.2 平台耦合不是全部靠词法扫描发现

初步扫描在 73 个入口中发现 Muse/Hatch 标记或平台绝对路径。这个数字仅是**启发式命中数**，不是“另外 15 个完全可移植”的证明。设备数据、研究委派等依赖可能通过其他工具名称表达。

真正的能力闭包应包括：Skill 正文、references、scripts、assets、外部命令、工具 schema、连接器账户、权限、网络与运行环境。每个待支持 Skill 都需要列清楚这些项目。

最优先打通邮件/日历读取与摘要、公开信息跟踪和本人通知。设备同步、广告、音乐、媒体生成等需要独立 provider；未实现时明确 blocked/unsupported，不假装可执行。

## 4. 可以复用的代码：逐项判断

本节“直接复用”仅指**技术可调用性**；公开发布许可另外判断。所有未证实许可证的原文件都不能自动进入公开源包。

### 4.1 第一优先级：hook 协议 helper

**文件：** `architecture/hooks/runtime/hatch_hook_runtime.sh`  
**规模：** 114 行 / 3,241 字节  
**SHA-256：** `c87af221181a1559e3adcb0cdd601f5be5d4bd91eec2fe0597c09dc27558e741`  
**依赖：** Bash、jq；状态路径与 hook id 环境变量由宿主提供。

| 原函数 / 行号 | 已有功能 | 技术复用建议 |
|---|---|---|
| `_hatch_hook_compact_json` 5–8 | 验证并压缩 JSON | 可使用 |
| `_hatch_hook_object_json` 10–13 | 限定 JSON object | 可使用 |
| `_hatch_hook_emit` 15–50 | 输出终结结果并退出 | 兼容保留；只在 child shell 中运行 |
| `silent` 52–54 / `wake` 56–58 | 简洁调用入口 | 可使用 |
| `disable_after_run` 60–62 | 标记本次后禁用 | 外层需与事件接纳原子提交 |
| `log` 64–80 | stderr 的结构日志 | 限流并脱敏 |
| `_hatch_hook_state_path` 82–85 | 拼接状态路径 | 原函数未验证 ID/path，外层必须验证 |
| `hook_state_get` 90–98 | 读取状态，异常返回空对象 | 损坏被吞掉；生产外层应区分损坏与首次运行 |
| `hook_state_set` 103–113 | JSON 验证、dry-run 跳过、临时文件 rename | 用 staging 包装，不能当完整事务存储 |

本次对**指定 hash 的 helper**进行了 8 项功能测试：silent、wake+disable、stderr log、初始状态、状态读写、dry-run 状态不变、拒绝非法 state 类型、拒绝非法 payload。它们都通过。没有运行机器专用健康检查、初始化或日志裁剪脚本。

### 4.2 直接使用之前必须补的内容

1. `silent/wake` 调用 `exit 0`。不得 source 到 SDK 主 shell，否则整个调用进程会退出。
2. 状态路径由环境拼接，hook id、invocation id 必须限制字符、长度并验证目录包含关系与符号链接。
3. `mv` 提供文件替换原子性，不提供并发状态事务与 event 入队的一致性，也不是 fsync 持久性证明。
4. 原 `hook_state_get` 将损坏或不可读文件视为空对象，可能重新触发首次检测或重复通知。应外层验证状态并报警。
5. 状态先写、wake event 后入队会产生丢检测窗口。必须使用 SPEC 的 staging + CAS + 单事务接纳方案。
6. dry-run 环境变量只跳过 helper 写状态，不能阻止其他文件/网络副作用；必须有真实隔离。
7. 超时、异常退出或错误格式不能自动当 wake；错误通知另设冷却。
8. 原 helper 可输出任意 JSON payload；新系统不能在未声明的情况下悄悄将 legacy payload 限定成 object。

私有原样副本已另行交付；主交接包中只提供原创 parser、事务例子及提取脚本。提取命令核验整个 ZIP 和文件 hash，不会从未知版本悄悄提取。

```bash
python reuse/extract_original.py /path/to/muse-sdk.zip /private/new-directory
python reuse/test_original_helper.py /private/new-directory/hatch_hook_runtime.sh -v
```

输出目录必须是新目录，且不加入 git、源码包或容器构建上下文。公开分发前仍需解决授权。

### 4.3 可改造的算法模式：keepalive-tripwire

`architecture/hooks/scripts/keepalive-tripwire.sh` 使用健康检查返回码、连续失败计数、健康时清零与 `silent/wake` 协议。

可吸收的是**廉价检测 → 阈值 → 唤醒**模式。不能说原脚本可独立工作，因为它依赖附件中不存在的机器脚本与固定路径。

其逻辑并非所有失败都连续三次才唤醒：返回码 1 会立即 wake；返回码 2 或脚本不可用才走阈值。超过阈值后没有 latch/cooldown，持续故障可能每次轮询都 wake。新实现要增加故障 episode id、一次报警 latch、恢复清零和冷却。

建议独立实现通用 `ConsecutiveFailureProbe`，参数为 checker、threshold、reset-on-success、cooldown 和 recovery rule；把稳定的故障 episode 转为业务 dedupe key。

### 4.4 不应直接搬走的脚本

**home-init.sh：** 行 14 在初始化成功前写 started 标记；行 16 虽捕获退出码，最终行 19 仍返回“done rc ok”。失败后的重试与上报语义不适合作为通用可靠初始化模板。

**log-trim.sh：** 行 16 执行外部脚本后没有按 exit status 判断；有输出就“trimmed”，无输出就“ok”，缺失依赖则 silent。错误可能被静默吞掉；不是完整可靠健康监控。

**runtime-cell 启动/停止/环境脚本：** 与沙盒 rootfs、mount namespace、socket、代理和平台二进制深度耦合。可借鉴 fail-closed 能力列表、overlay 和生命周期思路，不建议把原部署环境原样容器化后称为新 SDK。

**dynamic_credentials.py：** 是特定 Unix socket/authd 协议客户端，不是 OAuth 服务、通用密钥库或网络安全层。占位凭据与实际 token 解析依赖缺失后台；不得宣称复制此文件即可获得安全凭据托管。

### 4.5 条件性外围复用

`skills/artifacts/scripts/` 中有 `validate_xlsx.py`、`build_pptx.py`、`validate_pdf.sh`、`xlsx_formula_cache.py`、`recalc_xlsx.py`。它们可以作为对应 artifact 能力的待审候选，但依赖、资产、环境与授权需要分别核验；与 proactive 内核不是一回事，不应引入核心安装依赖。

媒体生成/处理 Python 与迁移脚本同理。静态语法通过不代表运行环境齐备、行为正确或可公开再分发。

## 5. 哪些是文档机制复用，哪些必须自研

| 类别 | 可借鉴/兼容内容 | 必须新增 |
|---|---|---|
| Scheduler | 五种 schedule、heartbeat/task 区分 | 调度算法、时区、DST、missed run、DB 事务 |
| Hooks | wire prefix、decision、disable、state helper | sandbox、超时、状态 staging、原子接纳、权限 |
| Context | 固定形状 brief、来源、sent/pending 视图 | 增量 sources、typed context、时效、隐私 |
| Decision | 静默优先与用户偏好约束 | 模型循环、schema、预算、提案及证据 |
| Actions | 本人通知、追踪、保存状态的行为口径 | grants、审批、outbox、回执与 unknown 对账 |
| Skills | 任务步骤与能力语义 | legacy normalization、工具兼容、账户授权、依赖闭包 |
| Memory | 偏好/事实/维护职责 | 可插拔持久实现；不要求 vector DB |
| Runtime | 能力可见性、生命周期分层 | 独立 daemon、部署、版本、迁移、conformance |

附件 self-improvement 等文档属于行为契约，不是已包含的记忆学习算法。首版可预留 maintenance job，不应在核心未完成时扩展成夜间自主改代码系统。

## 6. 授权、隐私与公开分发

### 6.1 没有发现整包许可

根目录没有 LICENSE/NOTICE。局部许可或来源说明包括字体相关 license 文本、航空公司 logo 的 notice/upstream、第三方来源 TOML 等。它们各自有适用范围，不能覆盖 Muse 的 Skill、平台文档或 shell runtime。

“网上可以找到”“可以在运行环境读取”“没有密钥”“有一份第三方 MIT 项目”都不能证明本附件获得开源再分发授权。GitHub 官方也区分了无许可仓库与授予开源使用权；详见 SPEC W13。本报告不是对法律状态的最终裁定。

建议分两条线：原创 SDK 代码按选定开源许可证发布；Muse 兼容测试素材由用户明确提供并留在私有位置。在正式捆绑任何原文前，取得相应权利证据并记录到 manifest。

本次提供的原创参考代码是本次编写，没有复制原 helper 的实现；这也不构成对任何第三方权利的最终法律保证。若商业或公开发行风险较高，应完成适当的来源与法律审查。

### 6.2 README 的“排除个人数据”不能当发布证明

尽管 README 表述已排除个人内容，归档任务里仍可见个性化任务安排和机器环境痕迹。因此不应直接上传整个 ZIP 到公开仓库。本报告不列出这些私人任务的内容，也没有将其作为公开 demo。

需特别排除：归档 cron、机器特有 hook 配置、环境/内部代理地址、用户偏好正文、业务账户示例、可能进入后续采集的日志与 token。没有执行全面 DLP/秘密检测，不宣称附件“已彻底无敏感信息”。

字体文件不在交接输出中；此处也没有转发原素材里的字体许可包或 logo 资产。

## 7. 复用决策表

| 对象 | 技术结论 | 本交接采取的方式 |
|---|---|---|
| hatch_hook_runtime.sh | 可直接调用，但必须套安全与事务适配 | 单独私有参考副本 + hash + 8 测试；未放入公开候选包 |
| Hook wire contract | 适合保持兼容 | 原创 strict parser 与测试 |
| 30 分钟 heartbeat 定义 | 可作为输入格式参考 | 重建通用 schema 和调度算法 |
| HEARTBEAT/偏好机制 | 值得保留 | 创建通用可配置模板，不复制私人内容 |
| 88 Skill 目录 | 可解析，不能一概执行或分发 | 元数据审计 + BYO importer 设计 |
| keepalive-tripwire | 有参考价值，不可脱离环境直接运行 | 建议通用 probe 重写 |
| home-init/log-trim | 存在语义缺口 | 不作为成品样板 |
| runtime-cell / authd client | 平台耦合，缺后台 | 不纳入核心；独立实现端口 |
| artifact / media 脚本 | 外围按需候选 | 留给后续专门依赖、行为与许可评审 |

## 8. 检验方法与局限

对 17 个 Python 文件完成 AST 语法解析，对 21 个 shell 文件完成 `bash -n`；未执行未知机器脚本。对唯一指定 hash 的 helper 做了临时目录中的功能测试。没有调用任何用户账户、实际邮箱/日历或 Muse 后台。

对 88 个 Skill 使用 YAML 解析和规则校验，并扫描平台标记；这不是全面的语义可移植性证明，也不是对每个引用链接与外部 API 的端到端认证。

运行与代码验证的完整记录见 `VALIDATION.md`。最终开源前仍须执行 SPEC P0–P7 的门禁。
