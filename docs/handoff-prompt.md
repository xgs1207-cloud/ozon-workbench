# 交给下一个 AI 的开场指令（直接复制粘贴）

> 用法：把下面 `---` 之间的整段发给新的 AI（Claude Code / Codex / 其它 agent 都行），
> 并确保它能访问这台电脑（`E:\抖音自动化项目\ozon-workbench`）。它自己会读文档、跑自检、认领任务。

---

你是接手一个**已在生产使用**的自动化项目的开发 agent。

**项目**：Ozon 上品自动化工作台（1688 采集 → 俄文文案 → 生图 → 真实 Ozon 类目属性 → 多店铺上架）。
**仓库**：`E:\抖音自动化项目\ozon-workbench`（git，main 分支；远端 `xgs1207-cloud/ozon-workbench`）。
**第一步只需做一件事**：完整阅读仓库根目录的 **`HANDOFF.md`**，然后按它的 §2「十五分钟自检」把本机与服务器两侧的测试都跑绿，
再用它 §2 里的 `pipeline.doctor` 与 `pipeline.stores --list` 看一遍当前状态。**在跑完这些之前不要改任何代码。**

读完 `HANDOFF.md` 后，请以它为准（它包含：代码地图、10 条铁律、真机踩过的所有坑、变更上线流程、未完成清单）。
特别注意这几条：

1. **密钥策略**：服务器的 `/etc/ozon-workbench.env` 里有 Ozon / 腾讯云 COS / 火山方舟的密钥。
   只能通过 `bash deploy/with-env.sh <命令>` 使用，**永不打印、永不写进代码或文档、永不提交**。
2. **真实提交是写操作**：会真的在你的 Ozon 店铺创建商品。必须显式确认
   （接口 `{"confirm":"SUBMIT"}`／CLI `--i-understand-this-hits-ozon`）才允许执行；干跑必须保持零写请求。
3. **卡在用户侧的事（需要人在浏览器操作、或需要人去控制台配密钥）→ 停下来，把步骤教给用户，
   不要继续闷头开发**。这是用户对本项目的明确要求。
4. **模型调用要省钱快捷**：默认关闭方舟思考链（`ARK_THINKING=disabled`），别开大重试。
5. **开发前先联网搜可用的 skill 再动手**；界面必须美观，参照官方
   `frontend-design` skill（<https://github.com/anthropics/skills/tree/main/skills/frontend-design>）：
   现有操作台 `web/console.html` 就是照它做的（工序导轨是唯一用力处、冷石墨 + Ozon 蓝单强调色、
   避开"米色+陶土 / 等高圆角卡片墙 / 逐段淡入 / 全大写小标签 / 圆点分隔元信息 / 按钮加箭头"这类 AI 默认感）。
6. **本机是 Windows PowerShell 5.1**：没有 bash/pwsh，内联 Python 极易被引号吃掉，而且
   **整段脚本解析失败会一行都不执行**（曾因此误判"已部署"）。要跑 Python 就写成文件传过去。

**认领任务**：从 `HANDOFF.md` §3.3 的清单里挑一件，按优先级推荐：
先做 #5（型号名称人工确认，最小、可直接验证），再做 #1（1688 真实采集，需要用户配合跑浏览器脚本）
与 #3（用真照片重跑生图）。每完成一件，都要给出**可复现的证据**（命令 + 输出 + HTTP 状态），
并按 §8 上线（本机测试 → 提交 → 传服务器 → 重启服务 → 服务器再跑一遍测试）。

如果你发现 `HANDOFF.md` 里的事实与现状不符（代码已被改动），**先更新文档再继续**——
这份文档是后续所有人和 AI 的唯一入口，保持它准确比多写代码更重要。

---

## 附：如果新 AI 只能联网、碰不到这台电脑

那就把仓库拉下来自己跑（公开仓库）：

```bash
git clone https://github.com/xgs1207-cloud/ozon-workbench.git
cd ozon-workbench
python -m unittest discover -s tests -p "test*.py"      # 751 OK（Python 3.11 全 passed；3.14 会 skip 一批）
```

但它**无法**完成需要真实环境的部分（服务器 `/opt/ozon-workbench`、`/etc/ozon-workbench.env` 里的密钥、
Ozon 店铺、COS 桶），那些必须由能访问本机/服务器的一方来做。这种情况请让它只做代码与文档层的改进，
并把需要真环境验证的事项列回 `HANDOFF.md` §3.3。
