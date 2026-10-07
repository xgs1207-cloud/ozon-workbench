# RightAPI 商品图片生成

工作台可把图片生成后端切换为 RightAPI，文本摘要、俄文文案和整套图片规划继续使用豆包火山方舟。已有豆包生图后端保留，可通过服务器配置切回；前端不保存或编辑服务密钥。

`gpt-image-2.5` 是用户提供的 RightAPI 接口模型标识。接入这一第三方标识不代表对模型来源、官方版本、价格或输出质量作出保证；以服务商的实际能力与账单为准。

## 配置

在服务器受保护、已被 Git 忽略的环境配置文件中设置以下内容。密钥仅在服务器填写，不写入代码、文档、前端或截图。

```dotenv
IMAGE_GENERATOR=rightapi
RIGHTAPI_API_KEY=replace-in-server-secret-file
RIGHTAPI_BASE_URL=https://www.rightapi.ai/draw/v1
RIGHTAPI_IMAGE_MODEL=gpt-image-2.5
RIGHTAPI_ASPECT_RATIO=3:4
RIGHTAPI_IMAGE_SIZE=2k
```

Web 服务若设置了 `WORKBENCH_WEB_IMAGE_GENERATOR`，它会覆盖 `IMAGE_GENERATOR`，应同时改为 `rightapi` 或删除旧覆盖值。生产环境 `/etc/ozon-workbench.env` 使用 `root:root / 600`，由 systemd 读取后向服务进程注入；CLI 须使用授权的环境加载方式，不放宽文件权限。项目的 `.env`、`.env.*`、`config/` 均已被 Git 忽略。更新配置后重启工作台服务。

保留 `ARK_API_KEY` 及既有文本模型配置。切回豆包生图时将有效后端设置为 `doubao`，并使用原有豆包图片模型配置。

## 请求契约

根据用户提供的接口示例，服务端向以下地址发起一次 `POST`：

`https://www.rightapi.ai/draw/v1/images/generations`

鉴权为 `Authorization: Bearer <RIGHTAPI_API_KEY>`，内容类型为 `application/json`。不复制示例中的 `Host`、`Connection` 或 Apifox 专用 `User-Agent`；这些不是业务字段。

```json
{
  "model": "gpt-image-2.5",
  "prompt": "当前图位已保存并审核的商品图片提示词",
  "images": ["https://public.example/reference-image.jpg"],
  "aspect_ratio": "3:4",
  "image_size": "2k",
  "response_format": "url"
}
```

`response_format` 固定为 `url`。当前适配器仅处理同步响应 `{"data":[{"url":"https://..."}]}` 的单张图片；未获得异步任务协议的公开依据，其他响应会停止并提示核对调用记录，不重新请求生图。

请求规格 `2k` 不代表工作台直接把供应商原始尺寸上传到 Ozon：输出通过现有图片流程保存为 `900×1200`，以保持当前商品图、质检和卡片编译的一致性；界面会同时说明请求规格和保存规格。

## 参考图与费用边界

参考图默认使用公开可读取的 HTTPS 原图地址，不承诺服务支持本地文件、内网地址或 `data:` URI。后台必须能把当前图位的参考图 ID 精确关联到该原图；旧记录只接受经过封存校验、可按采集器原始编号规则精确重建的关联，不能将成功下载列表与原图数组简单配对或猜测链接。

新采集记录保留图片文件与原图地址的精确关联；旧记录若只有本地文件、且无法可靠还原原图地址，需重新采集。工作台不会为此自动上传参考图到公开存储。缺少有效地址时，预检查会在发送生图请求前阻止操作，避免该次请求消耗生图费用。

先保存规格、事实、提示词与参考图，并确认整套图片规划，再点击某一图位的“生成 / 重做本张图”。确认框展示当前服务、模型、请求比例和保存尺寸；一次只生成该图位，不自动批量生成。

收费请求不自动重试。服务已接收请求但连接中断、超时或下载失败时，可能已经计费；先检查现有图位和服务商任务/账单，再决定是否重新生成。不把服务商错误视为“肯定没有扣费”，也不自动回退到另一家付费后端。

## 验证与上架

开发测试使用模拟接口验证配置、请求字段、参考图约束、返回下载与本地图片流程，不使用真实密钥进行付费生成；模型真实质量、服务端收费与具体账号可用性需要显式生成后确认。

生成完仍需人工检查所选规格、颜色、包装数量、外观和卖点是否一致，以及 Ozon 图片要求。生成完成不自动批准图片、不自动发布公开图片地址，也不自动创建 Ozon 商品卡。
