# 快手视频上传工具

运行一次，递归扫描 `E:\upload_test\` 的 MP4，串行处理未上传的视频。读取已有 `发布记录.xlsx`，以视频 SHA256 保存上传状态；同一个视频改名或移动后仍会跳过。

已激活 `.venv` 时，在项目目录运行：

```powershell
# 先看扫描结果：不启动浏览器、不调用模型、不改数据库和台账
python main.py --dry-run

# 自动扫描、查重、上传、提交并记录结果
python main.py
```

首次会打开项目独立浏览器，请手动登录快手，回到终端按 Enter 继续。每次命令只处理一轮，新增视频后再次运行即可。遇到验证码需要人工处理。

当前默认每轮只联调 1 集，优先第 1 集或已有作品。程序先关闭已知引导并点击右上角“内容上传”，在独立列表页查重，再按标签填写常规字段、选择附件。模型只处理无法自动定位的特殊控件，视频进度由程序轮询。调通后在 `.env` 设置 `UPLOAD_MAX_JOBS=0` 恢复批量。

模型单次请求上限默认 30 秒；参数 JSON 无效时最多重新生成一次，失败后停止本轮队列，后续视频保留待处理。具体阶段、耗时、异常、坏 JSON 原文和页面快照保存在 `.kuaishou-upload/logs/<任务ID>/`，API 密钥会从诊断信息中隐藏。

当前配置 `testMode=true` 是浏览器准备调试：会登录、填表并实际上传附件，最终提交前记录 `test_prepared` 后结束。平台可能保留草稿，台账不会写成已发布；重跑不重复上传已准备完成的附件。`--dry-run` 仍然只在本地生成资料。

不需要手工准备每集 JSON。程序扫描视频后自动识别剧名/集号、生成标题和简介、计算 SHA256、读取实际时长、截取视频帧作封面，并生成 `.kuaishou-upload/manifests/*.kuaishou.json`。已有同名 JSON 会作为可选资料读取，保留源文件。

备案号、版权证明、授权日期等固定资料只需在项目 `upload_config.json` 填写一次；首次运行自动从 `upload_config.example.json` 创建。免费模式是默认值，付费时修改 `sellingMode` 和 `episodePrice`。配置留空的剧名按文件名或目录识别，留空的封面自动截图。修改公共配置后重跑，全部单集 JSON 自动更新，无需逐集填写。

自动化流程：扫描 → 历史查重 → 自动生成资料 → 合并公共固定配置 → 平台查重 → 上传提交 → 保存结果。公共配置中的备案和版权资料需要使用实际值；程序不会从视频内容推断权属或生成证明文件。缺失项集中显示在 `.kuaishou-upload/待补资料.md`。

首次安装：

```powershell
uv venv --python 3.12
uv pip install -r requirements.txt
uvx browser-use install
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
.\.venv\Scripts\Activate.ps1
```

在 `.env` 填写中转站 `OPENAI_API_KEY`，默认 `OPENAI_BASE_URL=https://api.teamorouter.cn/v1`、`OPENAI_MODEL=deepseek-flash-free`。目录由 `UPLOAD_DIR` 配置，默认无需改。未激活虚拟环境时，将上面的 `python` 换成 `.\.venv\Scripts\python.exe`。

程序使用 `browser_use.Agent + Tools`：模型通过六个业务工具读取页面和规划操作，文件白名单、字段值、查重与提交状态由 Python 控制。已删除旧百度 demo、单文件命令入口、离线模拟器和内部重复规划流程。

状态保存在项目 `.kuaishou-upload\`，扫描报告为 `last_scan.json`，上传结果为 `last_run.json`。`--dry-run` 也会在项目内生成资料、媒体缓存和封面，方便检查，但不上传、不改视频源文件、数据库或台账。不要删除 `uploads.sqlite3` 或浏览器资料目录，以免丢失上传历史和登录状态。提交结果不明时下次只核对平台，不直接重发。Excel 被占用时先保存数据库，关闭 Excel 后重跑会同步台账。

详细行为与测试方法见 [运行说明](docs/kuaishou-upload.md)。目前已完成离线测试和本地浏览器表单验证，真实快手页面上传仍需在资料齐全后实测。


多平台架构、账号隔离、语义定位恢复及数据库迁移见 [多平台运行说明](docs/multiplatform-architecture.md)。目前真实业务适配器仍为快手；新增平台只扩展适配器，通用业务工具不重复增加。

# brower_auto_memory
