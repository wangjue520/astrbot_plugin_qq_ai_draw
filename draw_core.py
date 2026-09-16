# -*- coding: utf-8 -*-
"""核心逻辑（AstrBot 插件版）：配置 / 预设 / 角色字典 / LoRA / 提示词优化 / Forge / 队列。
与 QQ 平台完全解耦：配置由 main.py 注入，通知通过 job["notify"] / job["notify_image"] 回调。
移植自 QQ-AI-Draw-Bot (https://github.com/wangjue520/QQ-AI-Draw-Bot) 的 core.py。
"""

import asyncio
import base64
import json
import re
import time
from pathlib import Path

import httpx

# 由 init() 填充
CFG = {}
DATA_DIR = Path(".")
OUT_DIR = Path(".")
PRESETS_PATH = Path(".")
DICT_PATH = Path(".")
LORAS_PATH = Path(".")
PROMPT_PATH = Path(".")
HISTORY_PATH = Path(".")
USAGE_PATH = Path(".")
HISTORY = []
HISTORY_MAX = 100

DEFAULT_CONFIG = {
    "webui": {"base_url": "http://127.0.0.1:7860", "timeout": 600},
    "llm": {
        "base_url": "https://api.x.ai/v1",
        "api_key": "",
        "model": "grok-4",
        "proxy": "",
    },
    "gen": {
        "width": 1024,
        "height": 1024,
        "steps": 30,
        "cfg": 5,
        "sampler": "Euler a",
        "scheduler": "Automatic",
        "quality_prefix": "masterpiece, best quality, score_7, safe",
        "default_negative": "worst quality, low quality, score_1, score_2, score_3, artist name, blurry, jpeg artifacts, chromatic aberration",
    },
    "bot": {
        "cooldown_seconds": 3,
        "default_style": "默认",
        "daily_quota": 0,
        "max_pending_per_user": 2,
        "blacklist": [],
    },
    "lora": {"dir": "", "max_trigger": 2, "default_weight": 0.8},
}


def init(cfg: dict, data_dir: Path):
    """插件激活时由 main.py 调用：注入配置、初始化路径。"""
    global CFG, DATA_DIR, OUT_DIR, PRESETS_PATH, DICT_PATH, LORAS_PATH
    global PROMPT_PATH, HISTORY_PATH, USAGE_PATH, HISTORY

    CFG = json.loads(json.dumps(DEFAULT_CONFIG))  # 深拷贝默认配置
    for k, v in (cfg or {}).items():
        if k in CFG and isinstance(v, dict):
            CFG[k].update({kk: vv for kk, vv in v.items() if vv is not None})
        elif k in CFG:
            CFG[k] = v

    DATA_DIR = Path(data_dir)
    OUT_DIR = DATA_DIR / "outputs"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PRESETS_PATH = DATA_DIR / "presets.json"
    DICT_PATH = DATA_DIR / "char_dict.json"
    LORAS_PATH = DATA_DIR / "loras.json"
    PROMPT_PATH = DATA_DIR / "optimizer_prompt.txt"
    HISTORY_PATH = DATA_DIR / "history.json"
    USAGE_PATH = DATA_DIR / "usage.json"
    HISTORY = load_json(HISTORY_PATH, []) or []


# ========== 通用 ==========


def load_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def norm(s):
    """tag 归一化：小写、下划线转空格、压缩空白，用于匹配"""
    return re.sub(r"\s+", " ", s.lower().replace("_", " ")).strip()


def anima_tag(tag):
    """Danbooru 标准 tag（下划线）→ Anima 写法（空格）"""
    return tag.replace("_", " ")


def llm_available():
    key = CFG["llm"].get("api_key", "").strip()
    return bool(key) and "在这里" not in key


# ========== 画风预设 ==========


def load_presets():
    return load_json(PRESETS_PATH, {})


def save_presets(p):
    save_json(PRESETS_PATH, p)


# ========== 角色字典 ==========


def load_dict_entries():
    data = load_json(DICT_PATH, {"entries": []})
    return data.get("entries", [])


def save_dict_entries(entries):
    save_json(DICT_PATH, {"entries": entries})


def find_characters(text, entries=None):
    """在中文/英文文本里找角色，返回命中的字典条目（去重）"""
    if entries is None:
        entries = load_dict_entries()
    low = text.lower()
    norm_text = norm(text)
    hits, seen = [], set()
    for e in entries:
        if e["tag"] in seen:
            continue
        for name in e.get("names", []):
            name = name.strip()
            if len(name) >= 2 and name.lower() in low:
                hits.append(e)
                seen.add(e["tag"])
                break
        else:
            tag_anima = anima_tag(e["tag"]).lower()
            if tag_anima in norm_text:
                hits.append(e)
                seen.add(e["tag"])
    return hits


# ========== LoRA 管理 ==========


def parse_civitai_info(path):
    """解析 .civitai.info，提取角色触发词（括号限定词 tag）"""
    info = load_json(path)
    if not info:
        return None
    triggers = set()
    for words in info.get("trainedWords", []):
        for tok in words.split(","):
            tok = tok.strip()
            if "(" in tok and ")" in tok and len(tok) < 60:
                triggers.add(norm(tok))
    return {
        "file_name": info.get("fileName", ""),
        "model_name": info.get("modelName", ""),
        "triggers": sorted(triggers),
    }


def load_lora_overrides():
    return load_json(LORAS_PATH, {})


def save_lora_overrides(o):
    save_json(LORAS_PATH, o)


def scan_loras(lora_dir):
    """扫描 LoRA 目录，合并 civitai.info 与用户覆盖设置"""
    overrides = load_lora_overrides()
    found = {}
    d = Path(lora_dir) if lora_dir else None
    if d and d.is_dir():
        for info_path in d.glob("*.civitai.info"):
            parsed = parse_civitai_info(info_path)
            if not parsed:
                continue
            stem = info_path.name[: -len(".civitai.info")]
            ov = overrides.get(stem, {})
            found[stem] = {
                "stem": stem,
                "file_name": parsed["file_name"],
                "model_name": parsed["model_name"],
                "triggers": parsed["triggers"],
                "aliases": ov.get("aliases", []),
                "weight": ov.get("weight", CFG["lora"].get("default_weight", 0.8)),
                "enabled": ov.get("enabled", True),
            }
    for stem, ov in overrides.items():
        if stem not in found:
            found[stem] = {
                "stem": stem,
                "file_name": "",
                "model_name": ov.get("model_name", "（未扫描到）"),
                "triggers": ov.get("triggers", []),
                "aliases": ov.get("aliases", []),
                "weight": ov.get("weight", CFG["lora"].get("default_weight", 0.8)),
                "enabled": ov.get("enabled", True),
                "missing": True,
            }
    return found


def update_lora(stem, patch):
    ov = load_lora_overrides()
    cur = ov.get(stem, {})
    cur.update(patch)
    ov[stem] = cur
    save_lora_overrides(ov)


def detect_loras(final_prompt, user_text, loras=None):
    """在最终提示词 + 用户原文里匹配 LoRA 触发词/别名，返回 [(stem, weight, 命中词)]"""
    if loras is None:
        loras = scan_loras(CFG["lora"].get("dir", ""))
    norm_prompt = norm(final_prompt)
    norm_user = user_text.lower()
    hits = []
    for stem, lora in loras.items():
        if not lora.get("enabled") or lora.get("missing"):
            continue
        for trg in lora.get("triggers", []):
            if trg and trg in norm_prompt:
                hits.append((stem, lora["weight"], trg))
                break
        else:
            for alias in lora.get("aliases", []):
                if alias and alias.lower() in norm_user:
                    hits.append((stem, lora["weight"], alias))
                    break

    def _pos(hit):
        _, _, word = hit
        p = norm_prompt.find(word.lower())
        if p < 0:
            p = norm_user.find(word.lower())
        return (p if p >= 0 else 10**6, hit[0])

    hits.sort(key=_pos)
    max_n = int(CFG["lora"].get("max_trigger", 2))
    return hits[:max_n]


# ========== 提示词优化 ==========

DEFAULT_OPTIMIZER_PROMPT = """你是 Anima（CircleStone Labs 出品的 2B 动漫文生图模型，基于 Cosmos-Predict2）的提示词专家。
用户给你一句中文或英文的画面描述，你要改写成最适合 Anima 的英文提示词。

【Anima 提示词规则】
1. 模型用 Danbooru 标签 + 自然语言混合训练，推荐以标签为主，可穿插自然语言短句。
2. 标签一律小写；多词标签用空格连接、不用下划线（只有 score_9~score_1 这类评分标签用下划线）。
3. 标签排列顺序：[质量/年份/安全等元标签] [1girl/1boy/1other 等数量标签] [角色名] [作品名] [@画师] [一般标签]，组内顺序随意。
4. 画师标签必须加 @ 前缀（如 @quasarcake），否则效果很弱。绝对不要编造画师名；用户没点名画师就不要加画师标签。
5. 同一概念在 Danbooru 和 Gelbooru 写法不同时，优先用 Gelbooru 版本。
6. 需要强调时用权重语法 (tag:2)，Anima 需要的权重比 SDXL 更高。
7. 描述要具体充分：人物数量、发型发色、眼睛、服装、表情、姿势、构图、视角、背景、光影、色调。细节越足画面越稳，别怕长。
8. 用户提到角色名/作品名时保留官方原名；多角色时必须先报名字再逐个描述外貌，否则模型会混淆。
9. 不要输出质量标签（masterpiece、best quality、score_* 等）和安全标签（safe 等），也不要输出负面提示词，这些由系统统一添加。
10. 模型训练时做过 tag dropout，不需要堆砌所有相关标签，写关键细节即可。
11. 可以适当补充合理细节让画面完整，但不要偏离用户原意。
12. 如果用户输入本身已经是规范的英文 tag 串，只做整理和适度补全。
13. 如果用户消息里附带了【角色tag对照】，里面的角色必须严格使用给出的 tag 写法。

【输出要求】只输出最终英文提示词本体，一行或几行，禁止任何解释、引号、代码块、前后缀。"""


def get_optimizer_prompt():
    if PROMPT_PATH.exists():
        return PROMPT_PATH.read_text(encoding="utf-8")
    return DEFAULT_OPTIMIZER_PROMPT


async def optimize_prompt(user_text, char_hits):
    llm = CFG["llm"]
    hint = ""
    if char_hits:
        pairs = [f"{e['names'][0]} → {anima_tag(e['tag'])}" for e in char_hits]
        hint = "\n\n【角色tag对照】\n" + "\n".join(pairs)
    body = {
        "model": llm.get("model", "grok-4"),
        "messages": [
            {"role": "system", "content": get_optimizer_prompt()},
            {"role": "user", "content": user_text + hint},
        ],
        "temperature": 0.7,
    }
    proxy = llm.get("proxy", "").strip() or None
    async with httpx.AsyncClient(timeout=90, proxy=proxy) as cli:
        r = await cli.post(
            f"{llm['base_url'].rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {llm['api_key'].strip()}"},
            json=body,
        )
        r.raise_for_status()
        data = r.json()
    text = data["choices"][0]["message"]["content"].strip()
    return re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()


# ========== Forge 跑图 ==========


async def resolve_size_hint(hint):
    """把 1080p / 2k 这类模糊说法交给大模型，换算成模型能跑的分辨率"""
    llm = CFG["llm"]
    body = {
        "model": llm.get("model", "grok-4"),
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是图像分辨率转换器。把用户的分辨率描述换算成适合 SDXL 类模型的宽高像素。"
                    "硬限制：512~1536 之间、8 的倍数；超过上限就选同比例下能跑的最大值。"
                    "参考：1080p(16:9)→1344x768；720p→1216x704 或 1152x648；"
                    "2k/1440p→1440x808 或 1536x864；4k→1536x864；"
                    "竖屏按比例交换宽高；说法含糊就选 1024x1024。"
                    '只输出 JSON：{"width": W, "height": H}，不要任何其他内容。'
                ),
            },
            {"role": "user", "content": hint},
        ],
        "temperature": 0,
    }
    proxy = llm.get("proxy", "").strip() or None
    async with httpx.AsyncClient(timeout=120, proxy=proxy) as cli:
        r = await cli.post(
            f"{llm['base_url'].rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {llm['api_key'].strip()}"},
            json=body,
        )
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
    m = re.search(r"\{[^{}]*\}", text)
    if not m:
        raise RuntimeError("大模型没返回 JSON")
    d = json.loads(m.group(0))
    w = max(512, min(1536, int(d["width"])))
    h = max(512, min(1536, int(d["height"])))
    return [w, h]


async def wait_forge_idle(job=None, timeout=1800):
    """等 Forge 空闲再提交。本地 WebUI 手点的任务永远优先，机器人不抢。"""
    url = CFG["webui"]["base_url"].rstrip("/")
    warned = False
    t0 = time.time()
    while True:
        try:
            async with httpx.AsyncClient(timeout=10) as cli:
                r = await cli.get(f"{url}/sdapi/v1/progress")
                st = r.json().get("state") or {}
            if not st.get("job"):
                return
        except Exception:
            return  # 探测失败直接放行，交给 txt2img 自己报错/排队
        if not warned:
            warned = True
            if job is not None:
                await notify(job, "Forge 正在跑本地任务，排队等待中…")
        if time.time() - t0 > timeout:
            if job is not None:
                await notify(job, "Forge 长时间未空闲，直接提交排队")
            return
        await asyncio.sleep(2)


async def txt2img(prompt, negative, width, height):
    gen = CFG["gen"]
    payload = {
        "prompt": prompt,
        "negative_prompt": negative,
        "steps": int(gen.get("steps", 30)),
        "cfg_scale": float(gen.get("cfg", 5)),
        "width": width,
        "height": height,
        "sampler_name": gen.get("sampler", "Euler a"),
        "scheduler": gen.get("scheduler", "Automatic"),
        "seed": -1,
        "batch_size": 1,
        "n_iter": 1,
        "send_images": True,
        "save_images": False,
    }
    url = CFG["webui"]["base_url"].rstrip("/")
    timeout = int(CFG["webui"].get("timeout", 600))
    async with httpx.AsyncClient(timeout=timeout) as cli:
        r = await cli.post(f"{url}/sdapi/v1/txt2img", json=payload)
        if r.status_code != 200:
            raise RuntimeError(f"WebUI 返回 {r.status_code}：{r.text[:200]}")
        data = r.json()
    if not data.get("images"):
        raise RuntimeError("WebUI 没返回图片")
    return data["images"][0]


# ========== 任务队列 ==========

QUEUE = asyncio.Queue()
PENDING = []
CURRENT = None


def history_save():
    save_json(HISTORY_PATH, HISTORY[-HISTORY_MAX:])


def queue_status():
    return {
        "current": public_job(CURRENT) if CURRENT else None,
        "pending": [public_job(j) for j in PENDING],
        "history": HISTORY[-HISTORY_MAX:][::-1],
    }


def public_job(job):
    if job is None:
        return None
    return {
        k: v
        for k, v in job.items()
        if k not in ("notify", "notify_image", "_ws", "done")
    }


async def enqueue(job):
    PENDING.append(job)
    await QUEUE.put(job)
    return len(PENDING)


# ========== 配额 / 排队限制 ==========


def _usage_today():
    today = time.strftime("%Y-%m-%d")
    data = load_json(USAGE_PATH, {}) or {}
    if data.get("date") != today:
        data = {"date": today, "counts": {}}
    return data


def quota_remaining(user_id):
    """剩余每日配额；返回 None 表示不限额"""
    q = int(CFG["bot"].get("daily_quota", 0) or 0)
    if q <= 0:
        return None
    used = _usage_today().get("counts", {}).get(str(user_id), 0)
    return max(0, q - used)


def quota_consume(user_id):
    data = _usage_today()
    data.setdefault("counts", {})[str(user_id)] = (
        data.get("counts", {}).get(str(user_id), 0) + 1
    )
    save_json(USAGE_PATH, data)


def pending_of(user_id):
    """该用户尚未完成的任务数（排队中 + 正在跑）"""
    uid = str(user_id)
    n = sum(1 for j in PENDING if str(j.get("user_name")) == uid)
    if CURRENT is not None and str(CURRENT.get("user_name")) == uid:
        n += 1
    return n


async def notify(job, text):
    job.setdefault("logs", []).append({"time": time.strftime("%H:%M:%S"), "text": text})
    cb = job.get("notify")
    if cb:
        try:
            await cb(text)
        except Exception:
            pass


async def worker():
    global CURRENT
    while True:
        job = await QUEUE.get()
        if PENDING and PENDING[0] is job:
            PENDING.pop(0)
        else:
            try:
                PENDING.remove(job)
            except ValueError:
                pass
        CURRENT = job
        job["state"] = "running"
        job["start"] = time.time()
        job["done"] = asyncio.Event()
        try:
            await run_job(job)
            job["state"] = "done"
        except Exception as e:
            job["state"] = "failed"
            job["error"] = str(e)
            await notify(job, f"跑图失败：{e}")
            print(f"[跑图姬] 错误：{e}", flush=True)
        finally:
            job["done"].set()
            CURRENT = None
            QUEUE.task_done()


async def run_job(job):
    """完整生成管线：优化 → 拼提示词 → LoRA自动触发 → 跑图"""
    presets = load_presets()
    preset = presets.get(job.get("style") or "", {}) or {}
    user_text = job.get("content", "")
    gen = CFG["gen"]

    char_hits = find_characters(user_text)

    if job.get("mode") == "reroll":
        last = job.get("reroll_data")
        if not last:
            await notify(job, "你还没有跑过图，先画一张吧")
            return
        core = last["core"]
        job["style"] = last.get("style", job.get("style"))
        preset = presets.get(job["style"], {}) or {}
        job["size"] = last.get("size")
    elif job.get("mode") == "draw" and llm_available():
        await notify(job, "收到，正在让 AI 优化提示词…")
        try:
            core = await optimize_prompt(user_text, char_hits)
        except Exception as e:
            await notify(job, f"提示词优化失败（{e}），这次先用你的原文直接跑")
            core = user_text
    else:
        if job.get("mode") == "draw" and not llm_available():
            await notify(job, "（还没填大模型 key，跳过提示词优化）")
        core = user_text
    job["core"] = core

    parts = [
        gen.get("quality_prefix", "").strip().rstrip(","),
        preset.get("positive", "").strip().rstrip(","),
        core.strip().rstrip(","),
    ]
    prompt = ", ".join(p for p in parts if p)
    negative = gen.get("default_negative", "").strip()
    if preset.get("negative", "").strip():
        negative = (
            f"{negative}, {preset['negative'].strip()}"
            if negative
            else preset["negative"].strip()
        )

    # LoRA 自动触发
    job["loras"] = []
    lora_hits = detect_loras(prompt + ", " + core, user_text)
    if lora_hits:
        lora_tags = ", ".join(f"<lora:{stem}:{w}>" for stem, w, _ in lora_hits)
        prompt = f"{lora_tags}, {prompt}"
        names = "、".join(f"{stem}（命中:{hit}）" for stem, _, hit in lora_hits)
        await notify(job, f"自动触发 LoRA：{names}")
    job["loras"] = [stem for stem, _, _ in lora_hits]

    w, h = job.get("size") or (
        int(gen.get("width", 1024)),
        int(gen.get("height", 1024)),
    )
    if job.get("size_hint") and not job.get("size"):
        if llm_available():
            await notify(job, f"分辨率「{job['size_hint']}」交给 AI 决定尺寸…")
            try:
                job["size"] = await resolve_size_hint(job["size_hint"])
                w, h = job["size"]
                await notify(job, f"AI 选定分辨率：{w}x{h}")
            except Exception as e:
                await notify(job, f"分辨率识别失败（{e}），用默认尺寸")
        else:
            await notify(job, "没填大模型 key，模糊分辨率用默认尺寸")
    job["prompt"] = prompt
    job["negative"] = negative
    job["size"] = [w, h]

    notice = f"最终提示词：{prompt}"
    if len(notice) > 400:
        notice = f"最终提示词：{prompt[:300]}……"
    await notify(job, notice + f"\n开始跑图（{w}x{h}）…")

    await wait_forge_idle(job)
    b64 = await txt2img(prompt, negative, w, h)

    fname = time.strftime("%Y%m%d_%H%M%S") + ".png"
    (OUT_DIR / fname).write_bytes(base64.b64decode(b64))
    duration = round(time.time() - job["start"], 1)

    rec = {
        "time": time.strftime("%m-%d %H:%M:%S"),
        "file": fname,
        "prompt": prompt,
        "negative": negative,
        "style": job.get("style"),
        "size": [w, h],
        "source": job.get("source", "astrbot"),
        "user": job.get("user_name", ""),
        "loras": job["loras"],
        "duration": duration,
    }
    HISTORY.append(rec)
    history_save()
    job["result_file"] = fname
    job["duration"] = duration

    cb = job.get("notify_image")
    if cb:
        try:
            await cb(b64, fname)
        except Exception:
            pass  # 发送失败不算任务失败（图已存档）
    await notify(job, f"完成，耗时 {duration}s")
