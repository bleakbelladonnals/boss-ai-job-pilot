# BOSS AI 岗位投递 Skill

这是可分享安装的通用版，不包含原作者的账号、话术、筛选偏好、投递记录或本机路径。

## 安装

1. 解压压缩包。
2. 将完整的 `boss-ai-job-outreach` 文件夹复制到 Codex 的 Skills 目录。默认位置是 `~/.codex/skills/`；若设置了 `CODEX_HOME`，使用该目录下的 `skills/`。
3. 确认 Codex 中可以使用 `ego-browser` Skill。
4. 在浏览器 Profile 中自行登录 BOSS 直聘。不要把 Cookie、密码、验证码或完整手机号写入 Skill。
5. 首次使用时先要求只读测试；确认账号、筛选规则和完整话术后，再明确授权发送数量。

## 环境要求

- Codex Desktop 与可用的 `ego-browser` Skill。
- Python 3。
- macOS 或 Linux。保护脚本使用 POSIX 文件锁，Windows 原生环境尚未支持。

## 本地数据

账号配置、发送账本和运行证据保存在接收者自己的 `${CODEX_HOME}/boss-ai-job-outreach`，未设置 `CODEX_HOME` 时保存在 `~/.codex/boss-ai-job-outreach`。该目录不属于 Skill，不应发送给其他人。

## 自检

在 `scripts` 目录运行：

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_guard.py
```

测试只使用临时状态，不访问 BOSS，也不会发送消息。
