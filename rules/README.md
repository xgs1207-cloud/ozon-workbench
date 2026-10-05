# rules/ —— 从原项目 skill 提炼的可执行规则

这里的规则文档不是"参考读物"，而是要被 M2 / M3 的代码直接读进去当约束用的（例如拼 prompt、
或作为生成结果的后置校验）。每条规则都标注来源，区分 **Ozon 官方硬约束** 与 **项目自定风格**。

| 文件 | 覆盖内容 | 服务的里程碑 | 状态 |
|---|---|---|---|
| `ozon-title-description-rules.md` | 标题/简介/标签的长度、必含、禁含、俄文书写规范 | M2 | **已落盘**（含官方/项目自定/未验证三分） |
| `image-slot-and-qc-rules.md` | 图片槽位体系、每张图的设计目的与提示词要点、QC 判定与失败回退、上传侧图片要求 | M3 | **已落盘**（含我们的替换点：对象存储替代 24h 隧道） |

来源（原项目）：

- `.agents/skills/ozon-ecommerce-designer/SKILL.md`（约 50 KB，电商设计与文案规则主文件）
- `.agents/skills/ozon-ecommerce-designer/references/product-specific-image-standard.md`（约 9.5 KB，图片标准）
- `.agents/skills/image-planner/SKILL.md`、`.agents/skills/image-generator/SKILL.md`
- `scripts/russian_seo_rules.py`、`scripts/russian_color_rules.py`（俄文书写与颜色词规则）
- `templates/{title-ru,description-ru,copy-ru,keywords-ru,ozon-tags,rich-content,image-plan,image-qc-report}.schema.json`（字段级约束）

## 使用方式（约定）

1. 规则文档里的硬约束，**必须**变成代码里的校验（生成后不过校验就重试或转人工），不能只写在 prompt 里；
2. 规则有冲突时，Ozon 官方硬约束优先，项目自定风格可配置开关；
3. 后续如果拿到更新版本的规则，只改这两个文档 + 校验代码，不改生成侧的 prompt 结构。
