# -*- coding: utf-8 -*-
"""跑图姬 · AstrBot 插件版
群里 @机器人 "画一个佩丽卡" → AI 优化提示词 → 自动触发角色 LoRA → 本地 Forge 出图发回群里。
移植自 QQ-AI-Draw-Bot (https://github.com/wangjue520/QQ-AI-Draw-Bot)。
"""

import asyncio
import sys
import time
from pathlib import Path

# 确保能 import 同目录的 draw_core / draw_parse
_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

import astrbot.api.message_components as Comp  # noqa: E402
from astrbot.api import logger  # noqa: E402
from astrbot.api.event import AstrMessageEvent, MessageChain, filter  # noqa: E402
from astrbot.api.star import Context, Star  # noqa: E402

import draw_core  # noqa: E402
import draw_parse  # noqa: E402

DEFAULT_NEGATIVE = (
    "worst quality, low quality, score_1, score_2, score_3, artist name, "
    "blurry, jpeg artifacts, chromatic aberration"
)

LAST_JOB = {}  # user_id -> 上次跑图数据（重roll用）
LAST_TIME = {}  # user_id -> 冷却计时


class QQDrawPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._worker = None

        # 数据目录：优先放 AstrBot 数据目录（插件更新不丢失），拿不到再用插件目录
        data_dir = None
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            data_dir = (
                Path(get_astrbot_data_path())
                / "plugin_data"
                / "astrbot_plugin_qq_ai_draw"
            )
        except Exception as e:
            logger.warning(
                f"跑图姬：获取 AstrBot 数据目录失败（{e}），改用插件目录存数据"
            )
        if data_dir is None:
            data_dir = Path(__file__).resolve().parent / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        self._out_dir = data_dir / "outputs"
        self._out_dir.mkdir(exist_ok=True)
        self._seed_data(data_dir)
        draw_core.init(self._build_cfg(), data_dir)

    # ---------- 配置 ----------

    def _build_cfg(self):
        c = self.config

        def _s(key, default=""):
            v = c.get(key, default)
            return default if v is None else v

        return {
            "llm": {
                "base_url": _s("llm_base_url", "https://api.x.ai/v1"),
                "api_key": _s("llm_api_key", ""),
                "model": _s("llm_model", "grok-4"),
                "proxy": _s("llm_proxy", ""),
            },
            "webui": {
                "base_url": _s("forge_url", "http://127.0.0.1:7860"),
                "timeout": int(c.get("forge_timeout", 600) or 600),
            },
            "gen": {
                "steps": int(c.get("steps", 30) or 30),
                "cfg": float(c.get("cfg", 5) or 5),
                "sampler": _s("sampler", "Euler a"),
                "scheduler": _s("scheduler", "Automatic"),
                "quality_prefix": _s(
                    "quality_prefix", "masterpiece, best quality, score_7, safe"
                ),
                "default_negative": _s("default_negative", DEFAULT_NEGATIVE),
            },
            "bot": {
                "cooldown_seconds": float(c.get("cooldown_seconds", 3) or 0),
                "default_style": _s("default_style", "默认"),
                "daily_quota": int(c.get("daily_quota", 0) or 0),
                "max_pending_per_user": int(c.get("max_pending_per_user", 0) or 0),
                "blacklist": c.get("blacklist") or [],
            },
            "lora": {
                "dir": _s("lora_dir", ""),
                "max_trigger": int(c.get("lora_max_trigger", 2) or 2),
                "default_weight": float(c.get("lora_default_weight", 0.8) or 0.8),
            },
        }

    def _seed_data(self, data_dir: Path):
        """首次运行把角色字典/画风预设种子复制到数据目录（不覆盖已有）"""
        res = Path(__file__).resolve().parent / "resources"
        for name in ("char_dict.json", "presets.json"):
            src, dst = res / name, data_dir / name
            if src.exists() and not dst.exists():
                import shutil

                shutil.copyfile(src, dst)

    # ---------- 生命周期 ----------

    async def initialize(self):
        """插件激活：启动跑图队列后台任务"""
        self._worker = asyncio.create_task(draw_core.worker())
        logger.info("跑图姬插件已加载，跑图队列已启动")

    async def terminate(self):
        """插件停用/重载：停掉队列"""
        if self._worker:
            self._worker.cancel()
            self._worker = None

    # ---------- 消息入口 ----------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        msg = event.message_obj
        is_group = bool(getattr(msg, "group_id", ""))
        if is_group:
            at_me = any(
                isinstance(seg, Comp.At) and str(seg.qq) == str(msg.self_id)
                for seg in (msg.message or [])
            )
            if not at_me:
                return  # 群里没 @ 机器人，不掺和

        text = (msg.message_str or "").strip()
        if not text:
            return
        cmd = draw_parse.parse_command(text, draw_core.load_presets())
        if cmd["cmd"] == "unknown":
            return  # 不是画图指令，交给 AstrBot 正常流程（别的插件/LLM 处理）

        event.stop_event()  # 是画图指令，吃掉这条消息

        user_id = str(event.get_sender_id())
        if user_id in {
            str(x).strip() for x in draw_core.CFG["bot"].get("blacklist", [])
        }:
            return  # 黑名单：不搭理

        if cmd["cmd"] == "help":
            yield event.plain_result(draw_parse.HELP_TEXT)
            return
        if cmd["cmd"] == "styles":
            presets = draw_core.load_presets()
            lines = ["【可用画风】"] + [
                f"· {n}：{p.get('description', '')}" for n, p in presets.items()
            ]
            lines.append("用法：@机器人 画 画风名 画面描述")
            yield event.plain_result("\n".join(lines))
            return

        remain = draw_core.quota_remaining(user_id)
        if remain is not None and remain <= 0:
            yield event.plain_result(
                f"你今天的跑图配额已用完（每日 {int(draw_core.CFG['bot'].get('daily_quota', 0))} 张），明天再来吧"
            )
            return

        max_p = int(draw_core.CFG["bot"].get("max_pending_per_user", 0) or 0)
        if max_p > 0 and draw_core.pending_of(user_id) >= max_p:
            yield event.plain_result(
                f"你的任务还在排队/跑图中，跑完再发（每人最多 {max_p} 个未完成任务）"
            )
            return

        cooldown = float(draw_core.CFG["bot"].get("cooldown_seconds", 3))
        now = time.time()
        if now - LAST_TIME.get(user_id, 0) < cooldown:
            yield event.plain_result(f"太快了，{int(cooldown)} 秒内只能发一次任务")
            return
        LAST_TIME[user_id] = now

        job = {
            "source": "astrbot",
            "user_name": user_id,
            "time": time.strftime("%H:%M:%S"),
            "style": cmd.get("style")
            or draw_core.CFG["bot"].get("default_style", "默认"),
            "size": cmd.get("size"),
            "content": cmd.get("content", ""),
            "mode": cmd.get("mode", "draw"),
        }

        if cmd["cmd"] == "reroll":
            last = LAST_JOB.get(user_id)
            if not last:
                yield event.plain_result("你还没有跑过图，先 @机器人 画 一张吧")
                return
            job["mode"] = "reroll"
            job["reroll_data"] = last

        umo = event.unified_msg_origin

        async def notify_text(t):
            try:
                await self.context.send_message(umo, MessageChain().message(t))
            except Exception as e:
                logger.warning(f"跑图姬：通知发送失败：{e}")

        async def notify_image(_b64, fname):
            try:
                await self.context.send_message(
                    umo, MessageChain().file_image(str(self._out_dir / fname))
                )
            except Exception as e:
                logger.warning(f"跑图姬：图片发送失败（图已存 {fname}）：{e}")

        job["notify"] = notify_text
        job["notify_image"] = notify_image

        n = await draw_core.enqueue(job)
        draw_core.quota_consume(user_id)
        if is_group:
            yield event.chain_result(
                [
                    Comp.At(qq=event.get_sender_id()),
                    Comp.Plain(self._ack_text(n, remain)),
                ]
            )
        else:
            yield event.plain_result(self._ack_text(n, remain))

        # 记录供重roll（任务完成时由 run_job 填充 core）
        orig = job

        async def _remember():
            done_evt = orig.get("done")
            if done_evt is not None:
                await done_evt.wait()
            else:
                while orig.get("state") not in ("done", "failed"):
                    await asyncio.sleep(0.5)
            if orig.get("state") == "done":
                LAST_JOB[user_id] = {
                    "core": orig.get("core", ""),
                    "style": orig.get("style"),
                    "size": orig.get("size"),
                }

        asyncio.create_task(_remember())

    @staticmethod
    def _ack_text(n, remain):
        if n > 1:
            return f"已加入队列，前面还有 {n - 1} 个任务"
        if remain is not None and remain - 1 <= 5:
            return f"任务已受理（今日剩余配额 {remain - 1} 张）"
        return "收到，开始处理～"
