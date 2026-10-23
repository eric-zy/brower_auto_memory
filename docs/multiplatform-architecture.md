# 多平台上传架构

保留Browser Use 0.13.11，未增加Stagehand、Skyvern、LangGraph依赖。借鉴动作发现/执行分离、语义定位和检查点恢复。当前只有快手具有真实业务适配器；测试fixture不代表支持了第二个真实平台。

## 模块边界

| 模块 | 职责 |
| --- | --- |
| tools.py / contracts.py | 通用业务工具与参数，模型不能提交任意路径、脚本或事实值 |
| platforms/__init__.py | 注册各平台浏览器、工作流、资料加载器、生成器、身份计算和说明 |
| platforms/kuaishou_browser.py | 快手表单、域名、作品ID及上传结果证据 |
| platforms/kuaishou_workflow.py | 快手业务阶段与校验 |
| browser_runtime.py / page_state.py | 状态识别、受控动作、有限恢复和后置条件等待 |
| observation.py / locator.py | DOM语义快照、候选筛选、验证后的定位记忆 |
| recovery.py | 模型只从当前允许候选选择ref或null；可选截图辅助 |
| database.py / ledger.py | 平台/账号隔离、检查点、事件、Excel导出 |

browser.py保留旧导入兼容。新增平台实现独立适配器并注册AdapterSpec，复用tools，不套用快手表单或版权要求。资料加载器须返回标准任务接口：manifest、episodes、fields()、files、asset_hashes和fingerprint；平台规则由加载器和工作流验证。

## 状态与恢复

先区分登录、验证、协议、权限不足、加载、导航壳、列表、资料表单和视频上传状态。登录/验证码/法律协议/错误账号停止，不调用模型继续操作。

定位顺序为已验证语义记忆、名称/角色/标签匹配、有限模型恢复。入口、搜索、加集及下一步接入此机制；特殊字段仍通过业务工具核对。执行前重读DOM、链接、名称、可见性和遮挡。点击后等待目标状态，不能以点击调用成功代替业务结果。

每个任务默认最多2次定位恢复，同一URL/状态/意图只调用一次。字段值、附件白名单、查重和最终提交由程序控制。提交不走自动定位恢复；提交意图先落盘，结果不明时只对账、不重发。

只有核实后置条件后才缓存控件语义，缓存不含坐标、ref、Cookie或字段值。快照优先表单与按钮，最多600控件，报告截断与iframe数量。当前不提供跨域iframe或无DOM候选的坐标操作；此类页面停止并保存现场。

## 可选环境配置

```dotenv
UPLOAD_PLATFORM=kuaishou
UPLOAD_ACCOUNT_ID=default
UPLOAD_EXPECTED_ACCOUNT=
UPLOAD_RECOVERY_STEPS=2
UPLOAD_USE_VISION=false
```

现有.env可保持原样运行。新账号需使用不同的稳定UPLOAD_ACCOUNT_ID，并配置UPLOAD_EXPECTED_ACCOUNT为头部账号区域可核实的完整显示名或UID。正文出现账号名不算验证。默认账号为兼容历史记录可不配置，但建议绑定明确目标账号。

视觉仅在模型支持图像输入时启用，当前DeepSeek配置不能开启。关闭视觉仍可DOM语义恢复。视觉只能帮助从已有DOM候选中选择，不能点击任意坐标。

默认账号沿用原浏览器/日志路径；其他平台或账号的浏览器、语义记忆、生成资料、日志、截图和报告位于scopes/<身份摘要>/，不共用默认账号登录会话。媒体时长与视频封面缓存可按内容哈希共享。

## 数据与迁移

assets保存共享文件，upload_jobs以(platform,account_id,sha256)唯一；upload_series按平台/账号隔离作品ID。状态更新与事件落盘在同一事务完成。

首次写模式打开旧库时，以SQLite backup API生成uploads.sqlite3.before-multiplatform.sqlite3，再将旧videos/series一次性迁移到kuaishou/default。保留旧表和提交不明标记，不复制给新账号。预览只在内存迁移。

Excel新增平台/账号列；无身份的旧行只属于kuaishou/default。新账号不继承旧侧车中未注明账号的平台ID。

不要混用新旧版进程。旧表迁移后仅供审计，不再回写；回滚前先停止进程、核对新版产生的结果，不能直接用旧备份覆盖新状态。

## 本地验证

```powershell
python -B -m unittest discover -s tests -p 'test_*.py'
```

测试覆盖旧流程、迁移、账号/平台隔离、台账、脱敏页面回放、模型错误引用、遮挡和动作后状态检查，不执行真实上传。真实页面仍须在资料齐全后单独验证。
