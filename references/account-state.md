# 账号绑定与发送状态

`scripts/guard.py` 负责本地配置、互斥、配额、冷却、去重和恢复。它不访问 BOSS、不操作浏览器、不发送消息。任何发送入口都必须使用这份账本，不得另写不受保护的发送循环。

## 路径与本地数据

将当前已读取的 `SKILL.md` 所在目录记为 `SKILL_DIR`。调用保护工具时使用实际安装位置：

```text
python3 "SKILL_DIR/scripts/guard.py"
```

不要把 `SKILL_DIR` 当成字面路径，也不要假设安装用户名或固定绝对路径。Ego 的 Node 环境调用时，同样先把当前 Skill 的实际绝对路径写入 `guardPath`：

```js
const { spawnSync } = await import("node:child_process");
const guardPath = "当前 Skill 实际安装目录/scripts/guard.py";

function guard(request) {
  const result = spawnSync("python3", [guardPath], {
    input: JSON.stringify(request),
    encoding: "utf8"
  });
  if (result.error) throw result.error;
  const receipt = JSON.parse(result.stdout);
  if (result.status !== 0 || !receipt.ok) throw new Error(JSON.stringify(receipt));
  return receipt;
}
```

默认状态目录为 `${CODEX_HOME}/boss-ai-job-pilot`；未设置 `CODEX_HOME` 时使用 `~/.codex/boss-ai-job-pilot`。正式任务必须共用该目录；`--state-dir` 只用于隔离测试，不能用于绕过历史、停止状态或占用。

状态目录可能包含账号配置、发送记录和运行证据，不属于 Skill 分发包，不得分享给其他人。工具不保存 Cookie、Token、密码或验证码。

保护脚本使用 Python 标准库和 POSIX 文件锁，支持 macOS 与 Linux；Windows 原生环境需要另行适配锁机制，不能直接开始发送。

## 账号绑定

每个账号固定使用一个独立 Ego Profile。任务空间不能替代 Profile 的登录隔离。绑定字段：

- `accountKey`：稳定的本地标识，不能每批重命名。
- `profileId`：实际 Ego Profile ID。
- `identity`：页面可重复核对的脱敏身份标识。
- `greeting`：该账号已批准的完整话术。
- `filters`：该账号已批准的完整筛选快照。

账号身份和 Profile 绑定不可在已有记录上改写。需要给另一名求职者使用时，新建独立账号配置和 Profile，不继承话术或历史。

## 操作与字段

每次从标准输入传入一份 JSON 请求，标准输出返回一份 JSON 回执。`ok: false` 或非零退出码表示未获准继续。

共同的 `observed` 对象包含字符串形式的 `accountKey`、`profileId`、`spaceId`、`identity` 和 `city`。每次发送前从当前页面和任务空间重新观察，不复制旧观察值冒充实时检查。

| `op` | 必填字段与用途 |
| --- | --- |
| `configure` | `accountKey, profileId, identity, greeting, filters, approved: true, approvalEvidence`。首次注册或无未完成批次时更新完整配置。 |
| `start` | `accountKey, target, spaceId, observed, approved: true, approvalEvidence`。`target` 必须是用户本轮明确要求的正整数。 |
| `status` | 可带 `accountKey` 或 `runId`；只读返回筛选配置摘要、占用、冷却和待核验状态，不返回话术或身份。 |
| `prepare` | `runId, observed, recruiterId, jobUrl, candidateCity, checksPassed: true, historyChecked: true, evidence`。发送前取得一次性许可和完整话术。 |
| `resolve` | `runId, attemptId, result, observed, evidence`。保存真实发送结果。 |
| `halt` | `runId, reason`。停止批次并保留恢复状态。 |
| `resume` | `runId, observed, approved: true, approvalEvidence`。解决待核验项后，承接用户明确继续指令恢复。 |
| `finish` | 正常批次传 `runId, reason`；停止批次还需明确批准、执行已停止和最终核验证据。 |

`jobUrl` 必须是页面实际观察到的规范化 `https://www.zhipin.com/job_detail/<id>.html`；招聘者 ID 必须来自当前岗位或会话。

所有 `approved: true` 与 `approvalEvidence` 必须对应用户已经给出的明确要求，不能由 Agent 自己批准。

## 发送结果

`prepare` 会在网页动作前持久化 `sending`。一个账号存在 `sending` 或 `unknown` 时不能取得下一次许可。

| `result` | 证据与后续行为 |
| --- | --- |
| `confirmed` | 完整自定义话术与该条消息的“已发送/送达/已读”同时出现；计数一次并保留去重和冷却。 |
| `unknown` | 已点击或可能点击，但结果无法确认；停止账号，只读核验，不补发。 |
| `failed` | 页面明确显示本次消息发送失败；必须传 `failureVisible: true`，停止后等待用户明确继续。 |
| `unexpected_sent` | 平台默认招呼、不完整话术或其他非目标消息已经发出；不计成功，停止且不自动追加。 |
| `not_sent` | 能证明实际提交函数未调用；传 `clicked: false` 和执行阶段证据。仅“没看到消息”不构成证明。 |

`unknown` 只有在执行已停止，并分别取得“提交函数从未调用”和“首次入口未产生消息”的独立证据时，才能按原 `attemptId` 纠正为 `not_sent`。纠正不自动恢复批次。

## 恢复与结束

中断后先确认全部可能发送的执行已经终止，再读取原 `runId` 的 `status`。`sending` 按结果未知处理；在原账号、原 Profile、原任务空间只读核对。找到明确证据才 `resolve`，否则保留待核验状态。

正常完成或候选耗尽时，没有待核验项才执行 `finish`。异常停止的批次保留为 `halted`；只有用户明确继续并通过 `resume` 才能再次发送。不得新建批次、修改状态文件或更换状态目录绕过原记录。
