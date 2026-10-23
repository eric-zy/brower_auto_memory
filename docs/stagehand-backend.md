# B方案：Stagehand本地执行层

## 已实现

保留Python资料校验、任务数据库、台账与防重逻辑。默认浏览器执行后端改为Stagehand 3.7.3（Node >=22.18），Browser Use仅负责原有本地Chrome配置的启动和关闭。Node进程通过本机CDP连接同一任务标签页，不创建云浏览器，不迁移Cookie。

入口采用：已验证语义动作复用 → Stagehand.observe定位并验证实际节点 → Stagehand.act执行确定动作 → 观察真实表单后置条件。失败后交给Stagehand Agent完成多步导航；不再只让模型从外层菜单中选一次ref。

页面观察通过Stagehand原生Page.frames()/Frame.evaluate()/Frame.locator()进行。引用在不同iframe中全局唯一，填写、校验、文件选择会路由到对应Frame。支持同源和跨域框架；每一层仍检查可见性和授权域名。第三方或未授权域名仅记入诊断，不执行。业务平台域名策略仍在平台适配器中声明，不需要为每个iframe人工编写元素选择器。

特殊下拉等字段由Stagehand.observe查找位置，真实值仍交回Python业务工具填写和回读。下一步、查重、加集也使用完整框架快照和Stagehand定位。

## 执行边界

- Stagehand Agent的原生act/goto/fillForm/keys/click/type等工具全部移除，并在每步只激活4个受控导航工具。它不能填法律事实、选文件、最终提交、删除或切换账号。
- 文件白名单由Python提供，Node只接受video/cover/copyright角色，模型不能提供本地路径。
- 最终提交仍要求Python已落盘submissionAttempted，并通过现有资料、附件和作品ID复核。最终提交授权只能消费一次；testMode不授予此能力。
- 登录、验证码、协议、权限不足、页面错误均停止。初次登录仍由用户在原浏览器完成。
- RPC超时或身份错配时终止本任务Node进程，不重试页面动作。提交结果不明仍只对账，附件选择意图也保留。
- 模型服务失败停止本轮队列，避免对剩余视频重复消耗调用。

## 安装与运行

```powershell
cd E:\workSpace\brower-agent-test\stagehand
npm ci --ignore-scripts --no-audit --no-fund
cd ..
python main.py
```

Node依赖和package-lock.json固定版本。现有.env、上传数据库、版权资料及视频无需改动。默认：

```dotenv
UPLOAD_BROWSER_BACKEND=stagehand
```

旧执行器仅用于明确回退，可设UPLOAD_BROWSER_BACKEND=browser_use；不会在Stagehand故障后悄悄切回旧流程。

Stagehand默认复用OPENAI_API_KEY、OPENAI_BASE_URL、OPENAI_MODEL，并显式使用OpenAI兼容Chat Completions格式。可选用STAGEHAND_API_KEY、STAGEHAND_BASE_URL、STAGEHAND_MODEL单独配置。不在命令行传密钥，不将真实密钥写入源码或测试。

当前采用DOM模式，文本模型也能运行。视觉坐标代理未启用；这不会阻止原生跨域iframe操作。模型是否能稳定执行任务，仍取决于实际接口与模型能力；测试没有调用付费模型。

## 验证与限制

```powershell
python -B -m unittest discover -s tests -p 'test_*.py'
cd stagehand
npm test
```

隔离浏览器测试实际使用Chrome、Stagehand及跨域测试iframe，覆盖字段与附件在正确框架中的操作、防止导航工具提交、缓存复用、真实Agent多步调用和进程通信。Agent模型响应由本机模拟Chat Completions服务生成，不依赖公网模型。

真实平台上传与真实模型接口尚未验证。本次没有上传真实视频或更改运行数据库。当前业务适配器仍为快手，新增平台复用执行层，但必须提供其资料规则、账号范围及结果证据。
