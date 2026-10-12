# BOSS AI Job Pilot

> 可检查、可暂停、可恢复的 BOSS AI 岗位投递助手。

BOSS AI Job Pilot 是一个面向 Codex 的求职 Skill。它按照求职者确认的城市、岗位、薪资、经验、行业与招聘者活跃度逐岗筛选职位，在明确授权后发送指定话术，并用跨批次去重、发送账本和送达核验降低重复联系与误发送。

它适合希望保留筛选标准和发送控制权，同时减少重复浏览、机械核对与投递记录维护的人。

## 核心能力

- **逐岗审核**：读取完整职位名称与 JD，不以关键词命中代替岗位判断。
- **按规则筛选**：严格执行当前账号确认的城市、薪资、经验、行业和活跃度条件，候选不足时不擅自放宽。
- **授权后发送**：只使用求职者批准的完整话术，不调用平台默认招呼，也不临时改写。
- **跨批次去重**：按岗位、招聘者和会话历史保存发送记录，任务重启后仍能识别已联系对象。
- **结果核验**：只有完整话术与“已发送”“送达”或“已读”同时可见时才计为成功。
- **异常即停**：遇到验证码、登录失效、账号异常或结果不明时停止当前账号，并保留待核验状态。
- **多账号隔离**：账号、浏览器 Profile、任务空间和发送账本分别绑定，避免串号。

## 工作流程

```text
确认账号与规则 → 只读筛选 → 逐岗审核 → 明确发送授权 → 发送 → 核验 → 保存记录
```

“只读检查”和“实际发送”是两种独立模式。查看流程、测试登录或讨论规则不会自动获得发送权限。

## 适用范围

这个 Skill 用于 BOSS 直聘网页版上的 AI 产品岗位筛选、打招呼、送达核验和中断恢复。

它不负责修改简历，不把筛选规则泛化到其他招聘平台，也不会绕过验证码、账号异常或平台限制继续发送。

## 环境要求

- Codex Desktop
- 可用的 `ego-browser` Skill
- Python 3
- macOS 或 Linux。保护脚本使用 POSIX 文件锁，暂不支持 Windows 原生环境

## 安装

克隆仓库到 Codex Skills 目录：

```bash
git clone https://github.com/bleakbelladonnals/boss-ai-job-pilot.git ~/.codex/skills/boss-ai-job-pilot
```

如果设置了 `CODEX_HOME`，将仓库放到对应的 `$CODEX_HOME/skills/boss-ai-job-pilot`。

安装后确认 Codex 可以使用 `ego-browser`，再在独立的浏览器 Profile 中自行登录 BOSS 直聘。首次运行建议从只读检查开始。

## 快速开始

### 1. 只读检查

```text
使用 $boss-ai-job-pilot 检查当前 BOSS 登录状态和岗位筛选流程，先不要发送任何消息。
```

### 2. 确认筛选规则

```text
使用 $boss-ai-job-pilot 按我确认的城市、岗位方向、薪资、经验和招聘者活跃度筛选职位，列出符合与排除依据，不发送消息。
```

### 3. 开始一个明确批次

```text
使用 $boss-ai-job-pilot 按刚确认的规则和完整话术联系 10 个合格岗位，逐条核验送达；遇到验证码、账号异常或结果不明时立即停止。
```

发送前仍需在当前任务中确认账号、筛选条件、完整话术和批次数量。README 中的示例不会代替这些授权。

## 本地状态与隐私

账号配置、发送账本和运行证据默认保存在：

```text
${CODEX_HOME}/boss-ai-job-pilot
```

未设置 `CODEX_HOME` 时使用 `~/.codex/boss-ai-job-pilot`。

这些本地状态不属于 Skill 仓库。仓库不包含个人账号、Cookie、Token、密码、验证码、完整手机号、个人话术、简历文件或投递记录。

## 保护机制

`scripts/guard.py` 在每次可能发送前保存待处理状态并签发唯一许可，发送后再记录真实结果。冷却时间、配额、账号占用、历史去重与待核验记录会跨任务保留。

如果发送后页面超时、命令中断或回执缺失，该条记录会保持为结果未知。后续只能先进行只读核验，不能换入口补发或新建批次绕过。

## 项目结构

```text
.
├── SKILL.md                       # Skill 入口、模式判断与核心约束
├── agents/openai.yaml             # Codex 界面名称与默认提示
├── references/
│   ├── setup.md                   # 首次配置
│   ├── account-state.md           # 账号绑定、发送状态与恢复流程
│   ├── browser-workflow.md        # 浏览器操作流程
│   └── outsourcing-companies.md   # 可选的外包公司排除清单
└── scripts/
    ├── guard.py                   # 本地发送保护与状态管理
    └── test_guard.py              # 保护逻辑测试
```

## 自检

仓库当前包含 23 项保护测试。测试只使用临时状态，不访问 BOSS，也不会发送消息。

```bash
cd scripts
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_guard.py
```

也可以验证 Skill 的目录、名称与 frontmatter：

```bash
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py .
```

## License

[MIT](LICENSE)
