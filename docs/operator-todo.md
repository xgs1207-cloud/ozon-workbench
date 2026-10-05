# 需要你操作的事项清单（工作台唯一卡点）

> 本文件只列**必须由你操作**的事（控制台点击 / 密钥）。工作台代码侧已全部就绪、测试全绿。
> 每件事都给了「点哪里」与「5 秒自查命令」。做完一件回一句即可，我立刻验证并继续。

---

## 1. COS 写权限：给密钥所属子用户关联 CAM 策略 ⛔ 当前 403

**现象**（实测）：`PUT ozon-images/… → HTTP 403 AccessDenied`，连"列出账号名下的桶"也 403。

**根因**：密钥签名有效，但**该子用户没有关联任何 COS 权限策略**（不是桶策略、不是路径问题）。

**操作**
1. 打开 <https://console.cloud.tencent.com/cam> → **用户 → 用户列表**
2. 逐个点子用户 → **API 密钥** 标签 → 找到 SecretId 以 `AKIDALK9…` 开头的那一个
   - 若所有子用户里都没有 → 你建的是**主账号密钥**，而主账号不拥有这个桶 → 改用"能看到 `ozon-images-1486640018` 这个桶的账号"重新建密钥
3. 在该子用户页 → **关联策略** → 搜索 **`QcloudCOSDataFullControl`** → 勾选 → 确定
   （该策略=对 COS 数据的完整读写，不能改桶配置/权限，适合上传用途）

**自查**
```bash
cd /opt/ozon-workbench
bash deploy/with-env.sh .venv/bin/python deploy/cos-perm-diag.py
# 要看到 ✅ PUT 对象 @ 前缀 ozon-images/_ozon-workbench-diag-prefix.txt
```

---

## 2. COS 匿名读：让 Ozon 能抓到图 ⛔ 当前 403

**现象**：匿名访问图片地址 → `HTTP 403`（Ozon 抓不到图，上传也会被门禁拦）。

**操作**（桶 `ozon-images-1486640018` 页面内）
1. 左侧 **安全管理 → 阻止公共访问 → 关闭**（此开关会**压过**任何公开读策略）
2. **概览 → 基本信息 → 访问权限「私有读写」右边的 ✏️ → 公有读私有写**

**自查**（不需要密钥）
```bash
bash deploy/with-env.sh .venv/bin/python -m pipeline.oss_cos --probe --key ozon-images/__probe__.png
# 403 → 404 即算通过（404 = 对象不存在，但公开可读已生效）
```

---

## 3. 火山方舟：AI 文案 + 豆包生图

**操作**
1. <https://console.volcengine.com/ark> → **API Key 管理** → 新建 → 得到 `ARK_API_KEY`（文本与生图共用）
2. `ARK_TEXT_MODEL`：现在填的是占位值 `ep-2026xxxx`。改成
   - 你在"在线推理 / 接入点"里创建的**接入点 ID**（形如 `ep-2026xxxxxx-xxxxx`），或
   - 控制台**模型列表**里显示的直接可调用的**模型 ID**
3. `ARK_IMAGE_MODEL` 默认 `doubao-seedream-3-0-t2i-250415`：若账号里没有，换成可用的 seedream 版本

**我会先跑**：翻译 1 个词 + 生成 1 张图的最小请求，确认密钥与模型 ID 都对，再跑整链。

---

## 4. Ozon Seller API：真正上架

**操作**：Ozon Seller 后台 → **设置（Настройки）→ API 密钥（API-ключи）→ 创建密钥**（权限需含商品读写）；
同一页可看到 **Client-Id**（一串数字）。
→ `OZON_DEFAULT_CLIENT_ID` = 该数字，`OZON_DEFAULT_API_KEY` = 该密钥。

**我会先跑**：只读调用（拉类目树/属性）→ 上传**干跑**载荷给你看 → 你确认后才真提交。

---

## 密钥怎么放到服务器（两种，任选）

**A. 你自己填（推荐，密钥不经过我）**
```bash
ssh -i "D:\AI作图\ozonfinancedeploy.pem" ubuntu@43.132.190.110
sudo nano /etc/ozon-workbench.env      # 填对应变量；不要加引号、不要留空格
sudo systemctl restart ozon-workbench-api
```

**B. 写到本地文件我来搬**
把内容写成 `D:\AI作图\cos-keys.txt`（每行 `KEY=值`），跟我说"用 cos-keys.txt"。
我会 scp 到服务器写进 600/640 的 env，**随即删掉本地文件，全程不打印密钥内容**。

> ⚠️ 已经出现在聊天里的密钥（`AKIDnW5e…`、`AKIDALK9…`）建议用完后在 CAM 里禁用并重建。

---

## 顺序建议与「做到哪一步」

| 优先 | 事项 | 做完你会看到 |
|---|---|---|
| 1 | 第 1、2 项（COS 写权限 + 公开读） | 我把 10 张图传到 COS，并匿名复验 Ozon 能抓到 → 给你 https 地址 |
| 2 | 第 3 项（方舟） | 真的俄文标题/简介 + 真的豆包生图（替换占位图） |
| 3 | 第 4 项（Ozon） | 真类目/属性 + 干跑载荷 + 你确认后真提交 |

**你只要回一句"第 N 项好了"**，我就跑该步骤的验证并把结果贴给你；不用你再操作别的。
