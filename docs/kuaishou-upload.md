# 目录上传运行说明

## 日常运行

在项目目录激活虚拟环境后运行 `python main.py` 即可。程序递归扫描 `E:\upload_test\`，读取历史台账，自动生成单集资料并合并公共固定配置，再按剧名和集号串行处理。每集先核对平台，必要时填表上传，最后提交并核对详情页。相同剧复用已确认的作品 ID。第 1 集未建立作品、或者已有剧审核中/已完结时，后续集会阻塞，不创建重名作品。

只预览使用 `python main.py --dry-run`。预览会生成项目内单集 JSON、封面、媒体缓存和扫描报告，不写上传数据库、视频旁的源 JSON 或 Excel，不打开浏览器、不调用模型，也不上传附件。`python main.py` 根据公共配置执行：`testMode=true` 打开平台、登录、填表并实际传输附件，在最终提交前停止；`false` 完成正式提交，不另加提交参数。

命令只执行一轮，不是后台监控服务。可以重复运行；已经发布的、平台已存在的和本地确认已提交的内容会跳过。目录变化后重新扫描即可。

联调默认 `UPLOAD_MAX_JOBS=1`，优先第1集或已有作品。`python main.py` 不需要增加参数；调通后在 `.env` 将此项改成 `0` 恢复批量。达到本轮上限时，其余视频保留待处理，不当作失败。

入口点击和常规填表不依赖模型：程序关闭已知“知道了”引导、点击“内容上传”，确认表单出现后，在独立只读列表页精确搜索剧名。存在作品时核对详情和加集资格，无同名作品时才允许从第1集新建。普通输入、原生单选/下拉和可确定的附件位由程序执行；歧义或自定义控件交给模型。附件一旦传入就交回程序轮询，避免每次进度查询调用模型。

## 配置

在 `.env` 设置：

| 配置 | 默认值或用途 |
| --- | --- |
| `OPENAI_API_KEY` | 中转站密钥，必须填写 |
| `OPENAI_BASE_URL` | `https://api.teamorouter.cn/v1` |
| `OPENAI_MODEL` | `deepseek-flash-free`，使用文字/网页结构，不输入截图 |
| `UPLOAD_DIR` | `E:/upload_test` |
| `UPLOAD_CONFIG` | 项目中的 `upload_config.json`，公共固定资料 |
| `UPLOAD_LEDGER` | 视频目录中的 `发布记录.xlsx` |
| `UPLOAD_STATE_DIR` | 项目中的 `.kuaishou-upload` |
| `UPLOAD_HEADLESS` | `false`，首次登录需显示浏览器 |
| `UPLOAD_STABLE_SECONDS` | `30`，最近修改文件暂缓处理 |
| `UPLOAD_MAX_STEPS` | `80`，每集模型步骤上限，不是费用上限 |
| `UPLOAD_MAX_JOBS` | `1`，联调单集；`0` 为全部 |
| `UPLOAD_REQUEST_TIMEOUT` | `30`，模型单次请求秒数 |
| `UPLOAD_TIMEOUT` | `600`，视频上传等待秒数 |

默认关闭浏览器扩展，浏览器启动事件等待上限 120 秒、启动子进程等待上限 90 秒。首次请在打开的独立浏览器中手动登录，再回到终端按 Enter。运行中遇到验证码会停止本轮，处理后重跑。

## 目录与资料

```text
E:\upload_test\
  发布记录.xlsx
  某剧\
    第1集.mp4
    第2集.mp4
```

只扫描 MP4；忽略 `testdata`、`screenshots`、`.kuaishou-upload`、虚拟环境和 Git 目录，不跟随符号链接/目录联接。正在下载、有下载标记、空文件或最近仍在修改的文件暂缓处理。

不需要视频旁的 JSON。项目 `upload_config.json` 统一设置 `filing`、`copyright`、`sellingMode`、`episodePrice` 和平台固定选项；首次运行自动创建该配置模板，填写一次后复用于本批视频。默认免费，付费时修改公共配置；不同作品的备案/版权不同，应切换对应公共配置再运行。

初次需要填写的固定项是 `filing.number`、`copyright.proofType`、`copyright.proofFile`、`copyright.authorizedForUpload`、`copyright.validFrom` 和 `copyright.validUntil`。其中授权为布尔值，日期用 `YYYY-MM-DD`，文件用本地绝对路径。模型和视频不能提供这些固定资料的实际值，配置模板会留空；填写后无需逐集操作。页面额外选项放在 `platformFields`，每个键为页面字段名，值为所需选项。

模拟配置可设置 `testMode=true`，标记传给每份 JSON，但不在扫描阶段阻塞浏览器。程序照常登录、查重、填表并上传附件，准备复核完成后记录 `test_prepared`，不点击最终提交。平台可能保存未提交草稿；Excel 记录 `test_prepared`，不写为已发布。重跑同一份资料保留原草稿，不重复传附件。源 JSON 已标为测试时，公共配置中的 `false` 不会取消该标记。

程序生成 `.kuaishou-upload/manifests/<SHA256>.kuaishou.json`。集号优先使用明确的旧资料、`第N集` 或数字文件名；无法提取时使用同剧目录自然排序的序号。剧名优先使用公共固定剧名、已有资料、文件名中的剧名前缀，最后使用父目录名（支持提取《剧名》）。无简介时生成标识式简介“《剧名》第N集。”，不自动编造剧情、演员或权属。运行前用预览检查自动命名，必要时在公共配置固定剧名/简介。

SHA256 由实际视频计算，时长由 FFmpeg 读取。未指定封面时自动截取前半段视频帧，缓存于 `.kuaishou-upload/covers`，不传输给模型。媒体提取依赖随 `imageio-ffmpeg` 安装，无需单独安装系统 FFmpeg。参考 [依赖官方说明](https://github.com/imageio/imageio-ffmpeg)。

视频旁同名 JSON 是可选输入，已有的剧名/标题等非空资料会保留，公共配置中的非空固定值优先。支持旧 `coverPath`、`sale.mode`、集数键 `no`；来源说明和媒体尺寸作为辅助信息处理。未知的非空业务字段仍会报告待确认，避免静默丢失。源文件不修改；生成格式见 [输入约定](../skills/kuaishou-minidrama-upload/references/input.md) 和 [JSON Schema](kuaishou-manifest.schema.json)。

## 查重和中断恢复

SQLite 是运行状态的依据，Excel 是可查看的台账：

1. 已知 SHA256 的成功记录直接跳过，视频改名/移动仍有效。
2. 导入原台账的已发布记录；原台账无哈希时，仅按确切路径或全目录唯一文件名匹配。多条冲突记录会阻塞。首次同步前备份为 `发布记录.before-agent.xlsx`，保留原备注、其他工作表和自定义列。
3. 当前未确认的视频还需在平台核对剧名、作品 ID、目标集号及标题，不只相信历史 JSON。
4. 选择附件前、最终提交前先落盘意图；不明确的附件选择不会自动重复。提交超时进入 `unknown`，下次只核对；找不到明确结果时保持待核对，需人工确认后处理。
5. 同剧串行复用已确认 ID；审核中或已完结停止加集。模型报告完成不能替代工具取得的真实审核状态。审核通过与提交成功是不同状态，当前不定时刷新后续审核结果。

保留 `.kuaishou-upload/uploads.sqlite3`、旧版 `.kuaishou-upload/live` 记录及台账。程序会尝试导入旧版相同资料指纹的最终状态。若旧文件已被修改而无法匹配，会停止并要求核对。删除本地历史会削弱查重依据，尤其原台账没有哈希时，无法保证识别历史改名文件。

`upload.lock` 避免同时启动两个本项目进程。异常断电残留锁时，确认旧进程已经退出后再删除该锁；不要删除数据库来解决锁问题。Excel 占用不回滚已完成上传，下次启动会补同步。

## 工具与验证

只有外层 `browser_use.Agent` 调用模型。六个工具为 `scan_directory`、`inspect_platform`、`fill_form`、`upload_files`、`get_upload_status`、`submit_upload`；底层浏览器执行确定性 CDP 操作。模型选择字段和页面引用，不能提供任意值、任意路径、任意网址或 JavaScript。最终提交只能通过 `submit_upload`，工具先校验状态并落盘，再点击一次。

Agent 启用 `flash_mode`、关闭额外规划与失败后的最后一次自由回复，并缩短页面及历史上下文。JSON 解析或 schema 校验失败只重新请求模型一次，不重复页面动作；网络超时直接停止，SDK 不叠加网络重试。模型请求预算为每次 30 秒，Agent 模型步骤总预算相应覆盖两次请求；模型持续失败会停止整个批次，后续任务仍是待处理。

诊断目录 `.kuaishou-upload/logs/<任务ID>/` 包含 `events.jsonl`（阶段与耗时）、`last_error.json`（异常链）、`raw_arguments_*.txt`（坏 JSON 原文，仅解析失败时生成）、`page.json`（失败页面）及模型会话。凭据不写入请求日志，已知 API 密钥会被替换。原始响应可能包含业务资料，诊断目录已被 Git 忽略。每集结束立即保存 `last_run.json`，不必等整个队列结束。页面截图仍保存在 `screenshots`。

安装开发依赖并验证：

```powershell
uv pip install -r requirements-dev.txt
python -m unittest discover -s tests -q
python tests/browser_fixture_check.py
```

测试使用临时资料和本地 HTML，不连接快手、不调用模型 API、不上传真实视频。覆盖资料生成、公共配置合并、旧格式兼容、扫描、历史导入、重复内容、资料变化、提交中断、Excel 保留和工具注册；浏览器检查验证填写、回读、附件白名单和提交限制。真实平台选择器、字段和流程变化仍需实际账号校准，失败时保存现场并停止当前集。
