# 跑图姬（astrbot_plugin_qq_ai_draw）

QQ 出图机器人插件：群里 @机器人 说"画 画面描述"，AI 优化提示词 → 自动触发角色 LoRA → 本地 WebUI Forge 出图发回群里。

灵感与核心逻辑来自独立项目 [QQ-AI-Draw-Bot](https://github.com/wangjue520/QQ-AI-Draw-Bot)（同一作者）；插件版由独立版移植，QQ 接入改由 AstrBot 负责。

## 功能

- **AI 提示词优化**：大白话 → 规范英文提示词（默认 xAI grok，可换任何 OpenAI 兼容 API）
- **角色字典**：内置 5200+ 条「中文名 → Danbooru tag」，说"画一个佩丽卡"自动命中 `perlica (arknights)`
- **LoRA 自动触发**：扫描 LoRA 目录的 `.civitai.info`，命中训练词自动拼 `<lora:xxx:0.8>`（上限和权重可配）
- **画风预设**：自带示例画风，可改数据目录下的 `presets.json` 增删
- **分辨率**：`画 1000x1400 少女`（精准）、`画 1080p 少女`（模糊，AI 换算）、`画 横/竖/方`
- **队列与配额**：多任务排队、每人每日配额、冷却、并发上限、黑名单
- **网页手点优先**：Forge 网页里有人在跑，机器人自动排队

## 前置条件

1. 本地部署 **WebUI Forge** 并带 `--api` 启动。推荐用 [forge-webui-launcher](https://github.com/wangjue520/forge-webui-launcher) 一键部署。
   验证：浏览器打开 `http://127.0.0.1:7860/sdapi/v1/progress` 能显示 JSON。
2. 大模型 key（可选）：https://console.x.ai 生成，不填则跳过提示词优化直接跑原文。

## 安装

方式一（上架后）：AstrBot WebUI → 插件 → 插件市场 → 搜"跑图姬" → 安装。

方式二（手动）：把本仓库 clone 到 AstrBot 的 `data/plugins/` 目录，重启 AstrBot 或在插件页重载。

## 配置

安装后在插件卡片上点配置，填三项即可跑起来：

| 配置项 | 说明 |
|---|---|
| Forge 地址 | 默认 `http://127.0.0.1:7860` |
| LoRA 目录 | Forge 的 `models\Lora` 完整路径，留空关闭 LoRA 触发 |
| xAI API Key | `xai-` 开头，留空跳过提示词优化 |

其余步数 / CFG / 采样器 / 质量前缀 / 配额 / 冷却都有默认值。

## 使用

群里 **@机器人 + 指令**；私聊直接发指令（不用 @）。

| 指令 | 效果 |
|---|---|
| `@机器人 画 画面描述` | AI 优化 + 自动触发 LoRA + 跑图 |
| `@机器人 画 厚涂 横 一个少女` | 画风 + 构图自由组合 |
| `@机器人 生图 英文tag` | 跳过优化直接跑 |
| `@机器人 再来` | 同提示词换种子重 roll |
| `@机器人 画风` / `帮助` | 画风列表 / 说明 |

## 数据文件

首次运行会在 AstrBot 数据目录（`data/plugin_data/astrbot_plugin_qq_ai_draw/`）生成：

| 文件 | 作用 |
|---|---|
| `char_dict.json` | 角色字典（可手动增删条目） |
| `presets.json` | 画风预设 |
| `history.json` / `usage.json` | 历史出图 / 每日配额计数 |
| `outputs/` | 出图存档 |
| `optimizer_prompt.txt` | 自定义提示词优化 system prompt（生成后生效） |

## 免责说明

QQ 协议端属于第三方实现，存在账号风控/封禁风险，请仅用于小号并遵守相关法律法规与平台规则。本项目仅供学习与技术交流。
