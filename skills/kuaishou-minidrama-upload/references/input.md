# 输入约定

无需手工编写每集 JSON。`python main.py` 自动扫描视频，在项目 `.kuaishou-upload/manifests` 生成每集资料，自动提取哈希、时长、集号，生成标题/标识式简介和视频截图封面。公共固定资料在项目 `upload_config.json` 填一次，自动合并到每集。用 `python main.py --dry-run` 检查生成结果，不上传。

视频旁的同名 `*.kuaishou.json` 为可选旧资料输入，每份恰好一集。原文件只读；公共配置中的非空固定值优先。以下格式描述程序最终生成并校验的资料。

必填基础项：

- `seriesTitle`、`synopsis`：真实剧名与简介。
- `episodes[0].number`（兼容 `no`）、`title`、`sourceFile`。文件为非空 MP4 的绝对路径。
- `mediaInfo.sha256`：视频的 64 位 SHA256，程序重新计算比对；`durationSeconds` 大于零。当前不测媒体时长，使用用户声明。
- `copyright.proofType`、`proofFile`、`authorizedForUpload=true`。证明为非空的本地图片/PDF。
- `filing.hasNumber=true`、`number`。统一采用原 skill 实操段的 `V` 加 14 位数字；这是输入格式约束，不是对备案真实性的认定，平台变更后需校准。

真实平台另外必填：

- `coverFile`：本地封面图片绝对路径。
- `copyright.validFrom` / `validUntil`：YYYY-MM-DD，当前在授权有效期内。
- `copyright.allowSublicense`：布尔值，显式填写真实转授权选择。
- `sellingMode`：`free` 或 `paid`；付费要求 `episodes[0].paid=true` 且 `price>0`；免费要求 `paid=false, price=0`。
- `platformFields`：页面额外必填项的名称和值，例如分类、演员等；不得覆盖程序内置字段。未提供的必填项只能报告缺失。

`platformHistory.contentId` 和 `uploadedEpisodes` 只作为历史提示，不能替代页面查重。视频移动后允许用扫描到的实际路径替代旧 `sourceFile`，前提是 SHA256 一致；不会修改原 JSON。已有平台操作后，资料或附件改变会停止恢复。

`testMode=true` 允许浏览器准备调试：正常登录、查重、填表并上传附件，完成后记录 `test_prepared`，在最终提交前结束。不会记为已发布；重跑保留准备记录，不重复上传。备案、版权、日期从公共配置读取，不推断或编造。支持旧版 `coverPath` 和 `sale.mode`；`metadataSources/preparation` 与媒体尺寸不参与表单；`content/finished/fullScene/productionMethod/authorDeclaration` 的空占位会忽略，有值时需明确映射到 `platformFields`。集号缺失时可从明确的标题/文件名提取，冲突时停止。空价格仅在明确免费模式下归零。

结构示例（占位值需替换为真实资料，不能直接运行）：

```json
{
  "seriesTitle": "真实剧名",
  "synopsis": "真实简介",
  "episodes": [{"number": 1, "title": "第1集：真实标题", "sourceFile": "E:/upload_test/某剧/第1集.mp4", "paid": false, "price": 0}],
  "mediaInfo": {"durationSeconds": 120, "sha256": "替换为视频的64位SHA256"},
  "copyright": {"proofType": "与页面选项一致的真实类型", "proofFile": "E:/upload_test/某剧/版权证明.pdf", "authorizedForUpload": true, "validFrom": "2026-01-01", "validUntil": "2027-12-31", "allowSublicense": false},
  "filing": {"hasNumber": true, "number": "替换为真实备案号"},
  "coverFile": "E:/upload_test/某剧/封面.jpg",
  "sellingMode": "free",
  "platformFields": {},
  "platformHistory": {"contentId": null, "uploadedEpisodes": []},
  "testMode": false
}
```
