# -*- coding: utf-8 -*-
"""指令解析（纯函数，无第三方依赖，便于测试）。
移植自 QQ-AI-Draw-Bot (https://github.com/wangjue520/QQ-AI-Draw-Bot) 的 qq.py。
"""

import re

HELP_TEXT = (
    "【跑图姬使用说明】\n"
    "@机器人 画 画面描述 —— AI自动优化提示词后跑图\n"
    "@机器人 画 厚涂 横 一个少女 —— 画风(可省) + 构图随意组合\n"
    "@机器人 画 1000x1400 描述 —— 精准分辨率，直接生效\n"
    "@机器人 画 1080p 描述（480p~4k都行）—— 模糊分辨率，AI自动换算\n"
    "@机器人 生图 英文tag —— 不优化，直接按你写的tag跑\n"
    "@机器人 再来 —— 用上一条提示词重roll一张\n"
    "@机器人 画风 —— 查看可用画风列表\n"
    "@机器人 帮助 —— 本说明\n"
    "示例：@机器人 画 厚涂 横 1080p 一个在雨中撑伞的少女"
)

SIZES = {
    "横": (1344, 768),
    "横图": (1344, 768),
    "竖": (768, 1344),
    "竖图": (768, 1344),
    "方": (1024, 1024),
    "方图": (1024, 1024),
}

# 精准分辨率：出现在任意位置都直接解析，不调大模型
SIZE_ANYWHERE_RE = re.compile(r"(\d{3,4})\s*[x×*]\s*(\d{3,4})")
# 模糊分辨率：交给大模型判断
FUZZY_SIZE_TOKENS = ("1080p", "1440p", "900p", "720p", "540p", "480p", "2k", "4k")

KW_SEP = " ,，:：、　\t"


def _clean(text):
    return re.sub(r"\s+", " ", text).strip()


def extract_size_anywhere(text):
    m = SIZE_ANYWHERE_RE.search(text)
    if not m:
        return None, text
    w = max(512, min(1536, int(m.group(1))))
    h = max(512, min(1536, int(m.group(2))))
    return (w, h), _clean(text[: m.start()] + " " + text[m.end() :])


def extract_fuzzy_size(text):
    for tok in FUZZY_SIZE_TOKENS:
        m = re.search(r"(?<![a-z0-9])" + re.escape(tok) + r"(?![a-z0-9])", text, re.I)
        if m:
            return tok.lower(), _clean(text[: m.start()] + " " + text[m.end() :])
    return None, text


def parse_command(text, presets):
    """把消息文本解析成指令字典。"""
    t = text.strip().lstrip("/").strip()
    if not t:
        return {"cmd": "help"}
    if t in ("帮助", "help", "菜单", "功能"):
        return {"cmd": "help"}
    if t in ("画风", "风格", "styles", "style"):
        return {"cmd": "styles"}
    for kw in ("再来一张", "再画一张", "再来", "重roll", "reroll"):
        if t == kw or t.startswith(kw):
            return {"cmd": "reroll"}
    # 支持「画风X 画 …」顺序：先消费开头带分隔符的画风，再找动词
    lead_style, rest = match_style(t, presets)
    if lead_style:
        cmd = _parse_draw_verb(rest, lead_style, presets)
        if cmd:
            return cmd
    cmd = _parse_draw_verb(t, None, presets)
    if cmd:
        return cmd
    return {"cmd": "unknown"}


def _parse_draw_verb(t, lead_style, presets):
    """匹配开头的作画动词（画/生图/draw/raw…），剩余交给 build_draw；
    若 build_draw 没识别到画风，则用 lead_style（「画风X 画…」的写法）。"""
    for kws, mode in (
        (("生图", "原tag", "raw"), "raw"),
        (("画", "draw", "绘图"), "draw"),
    ):
        for kw in kws:
            if t == kw:
                return {"cmd": "help"}
            if t.startswith(kw):
                rest = t[len(kw) :].lstrip(KW_SEP).strip()
                if not rest:
                    return {"cmd": "help"}
                cmd = build_draw(mode, rest, presets)
                if lead_style and cmd.get("cmd") == "draw" and not cmd.get("style"):
                    cmd["style"] = lead_style
                return cmd
    return None


def build_draw(mode, rest, presets):
    """循环消费开头的修饰 token（画风/横竖方/精准分辨率/模糊分辨率），
    与顺序无关；剩下的就是画面描述。优先级：精准/横竖方 > 模糊分辨率。"""
    style, size, hint = None, None, None
    rest = rest.strip()
    while rest:
        s2, r2 = match_style(rest, presets)
        if s2:
            style = s2
            rest = r2
            continue
        sz, r2 = match_size(rest)
        if sz:
            size = sz
            rest = r2
            continue
        sz, r2 = extract_size_anywhere(rest)
        if sz and size is None:
            size = sz
            rest = r2
            continue
        h, r2 = extract_fuzzy_size(rest)
        if h:
            hint = h
            rest = r2
            continue
        break
    if size is not None:
        hint = None
    if not rest:
        return {"cmd": "help"}
    return {
        "cmd": "draw",
        "mode": mode,
        "style": style,
        "size": size,
        "size_hint": hint,
        "content": rest,
    }


def match_style(rest, presets):
    """匹配开头的画风名（长名优先），返回 (画风名, 剩余文本)"""
    for name in sorted(presets.keys(), key=len, reverse=True):
        if rest.startswith(name):
            tail = rest[len(name) :]
            if not tail:
                return name, ""
            if tail[:1] in (" ", "　", ",", "，"):
                return name, tail[1:].strip()
    return None, rest


def match_size(rest):
    for kw in sorted(SIZES.keys(), key=len, reverse=True):
        if rest.startswith(kw):
            tail = rest[len(kw) :]
            if tail[:1] in (" ", "　", ",", "，"):
                return SIZES[kw], tail[1:].strip()
    m = re.match(r"^(\d{3,4})\s*[x×*]\s*(\d{3,4})[\s，,]+", rest)
    if m:
        w = max(512, min(1536, int(m.group(1))))
        h = max(512, min(1536, int(m.group(2))))
        return (w, h), rest[m.end() :].strip()
    return None, rest
