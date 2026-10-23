# Midscene 正式项目接入

入口、扫描、资料提取、SQLite/Excel台账、文件哈希去重、模拟准备、最终提交和后续审核核对沿用原实现；新增独立 `midscene` 执行后端。默认按同文件夹同剧批量导入，旧逐集队列可用 `--upload-mode sequential` 保留；详见 `docs/folder-batch.md`。不会迁移或清空旧状态。

## 安装和运行

```powershell
cd E:\workSpace\brower-agent-test\midscene
npm ci --ignore-scripts
cd ..
python main.py
```

当前项目 `.env` 设置 `UPLOAD_BROWSER_BACKEND=midscene`。`UPLOAD_HEADLESS=false`（默认值）显示操作窗口；设置为 `true` 隐藏。新后端复用原 `.kuaishou-upload/browser-profile` 的登录会话及同一任务标签页；按批次固定文件白名单，整轮只由原有浏览器池关闭 Chrome。

原有运行参数继续有效：

```powershell
python main.py --dry-run
python main.py --directory "E:\upload_test\目标剧集目录" --max-jobs 0
python main.py --backend stagehand
python main.py --backend browser_use
```

`--backend` 只覆盖本轮。长期回退修改 `.env` 的 `UPLOAD_BROWSER_BACKEND`；不要删除数据库、台账、原始 JSON 或旧后端。

## 模型与缓存

- 固定 Midscene 1.16.5、Playwright 1.58.2；Node >=22.18。
- 默认复用 `OPENAI_MODEL / OPENAI_API_KEY / OPENAI_BASE_URL`。可用 `MIDSCENE_MODEL_NAME / MIDSCENE_MODEL_API_KEY / MIDSCENE_MODEL_BASE_URL` 单独覆盖；`MIDSCENE_MODEL_FAMILY` 必须对应实际模型。
- 当前 DeepSeek 路由使用 `MIDSCENE_MODEL_FAMILY=deepseek`，默认原生思考与 Deep Locate。无需打开旧后端的 `UPLOAD_USE_VISION`。
- 缓存位于当前平台/账号状态目录的 `midscene/cache`，按引擎版本、平台域名、页面账号区域及模型配置隔离。token 与运行报告在 `midscene/runs`，SQLite事件也记录已验证缓存的模型用量。
- 原生缓存以只读模式加载，确认当前上传表单或精确字段回读后才保存；当前文件与字段值由 Python 每次注入。缓存不保存历史上传成功、审核结果或完整文件上传请求。
- 缓存点击后仍验证实际表单，失败时只允许一次重新规划。没有验证的流程不晋升。页面变化仍可能导致缓存未命中，不保证零 token 或跨平台免适配。
- 模型402/429、连接失败等停止当前批次，不自动切换付费模型。依赖安装不等于模型服务可用。

默认按模型实际返回时间等待：`MIDSCENE_WAIT_FOR_MODEL=true`。SDK模型请求硬超时、HTTP客户端计时器和视觉导航总时限都关闭，旧的90/180秒环境配置不再提前终止模型。Python对可能调用模型的RPC持续等待，每30秒显示等待状态，不重新发送请求；Ctrl+C可中断，结果不明时仍须先对账。普通页面动作、非模型RPC、视频传输超时和提交守卫保持原规则。第三方服务主动断开、网络报错或返回错误时仍停止，无法延长服务器自身限时。

如需主动恢复固定限时，可在 `.env` 设置（单位毫秒）：

```dotenv
MIDSCENE_WAIT_FOR_MODEL=false
MIDSCENE_MODEL_TIMEOUT=90000
MIDSCENE_NAVIGATION_TIMEOUT=180000
```

固定限时模式下导航预算必须大于单次请求预算，不配置导航时会随请求预算增长，外层另留45秒处理直接入口探测和清理。默认关闭SDK模型语义重试，保留HTTP失败熔断，避免重复请求。

“内容上传”等入口存在唯一明确可操作控件时先直接点击并核实真实元数据表单，无需调用模型；入口不明确或点击后未出现表单时才使用原来的视觉恢复。仍遵守账号、协议、文件及提交守卫，点击成功本身不是表单已打开的证据。

## 保留的执行边界

Midscene 的原生 Agent 只获得受控 Tap/Scroll 导航动作，且缓存回放也经过相同守卫。视觉定位结果必须绑定到当前可核实的 DOM/frame 控件；找不到唯一对应关系即停止。它不能生成填写值、文件路径、JavaScript、任意导航地址或触发发布。

Python继续负责真实资料校验、固定值填写回读、附件SHA-256与角色白名单、选择附件前持久保存意图、提交前持久保存意图，以及实际结果核验。账号、验证码、新法律协议与授权确认仍停给用户。`testMode=true` 保持模拟准备，不能正式提交审核。

既有作品的查重先读完整集数分页，核实作品ID、已上传总量及终页；清单不完整不推断目标集缺失。“是否完结：是”会阻止继续加集。只有具体目标集的当前证据能记为已存在，不会依据51/51就把所有本地哈希标记为已上传。

## 验证

```powershell
python -m unittest discover -s tests -q
cd midscene
npm test
node probe-local.mjs
```

`npm test` 使用独立无头 Chrome、拦截 HTTPS 的本地合成页及临时状态，测试 frame、引用、附件白名单、协议/账号守卫、缓存回放守卫、分页和真实Python桥接；不请求模型服务。

`probe-local.mjs` 调用当前模型，但所有浏览器请求都被拦截为合成测试页，附件白名单为空。不使用真实账号，不上传/提交；测试同一Chrome与标签页上的冷启动、第二个worker的缓存回放和不同字段参数。报告位于 `midscene/test-runs/live-worker-*/report.json`。

接入时本地契约测试通过，但正式worker模型联调收到服务端402：当前 `deepseek-flash-free` 今日免费额度耗尽。未自动切换付费模型。服务额度恢复后可重跑该probe，再选择资料真实、未上传的新视频进行实际平台验收；不应使用已完结作品或测试版权/备案资料直接提交。
