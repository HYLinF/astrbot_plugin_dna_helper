"""新 main.py 的本地冒烟测试：用桩模块模拟 AstrBot 环境，验证核心逻辑不丢功能。"""
import asyncio
import base64
import importlib.util
import json
import os
import sys
import types

WORK_DIR = os.path.dirname(os.path.abspath(__file__))
MAIN_PATH = os.path.join(WORK_DIR, "main.py")

# ---------- 桩：astrbot ----------
logger = types.SimpleNamespace(
    info=lambda *a, **k: print("[INFO]", *a),
    warning=lambda *a, **k: print("[WARN]", *a),
    error=lambda *a, **k: print("[ERROR]", *a),
)

class FakeMessageChain:
    def __init__(self):
        self.text = None
        self.image_base64 = None
    def message(self, text):
        self.text = text
        return self
    def base64_image(self, b64):
        self.image_base64 = b64
        return self

class FakeEvent:
    def __init__(self, message_str="", unified_msg_origin="QQ_BOT:GroupMessage:1"):
        self.message_str = message_str
        self.unified_msg_origin = unified_msg_origin
        self.results = []
    def plain_result(self, text):
        self.results.append(text)
        return text

class FakeContext:
    def __init__(self):
        self.sent = []
    async def send_message(self, session, chain):
        self.sent.append((session, chain))
        return True

class FakeAsyncIOScheduler:
    def __init__(self, timezone=None):
        self.running = False
        self.jobs = []  # [(fn, kwargs)]
        self.timezone = timezone
    def add_job(self, fn, trigger=None, **kwargs):
        self.jobs = [(f, kw) for f, kw in self.jobs if kw.get("id") != kwargs.get("id")]
        self.jobs.append((fn, kwargs))
    def start(self):
        self.running = True
    def shutdown(self, wait=False):
        self.running = False
    def get_job(self, job_id):
        for fn, kw in self.jobs:
            if kw.get("id") == job_id:
                return kw
        return None
    def remove_job(self, job_id):
        self.jobs = [(f, kw) for f, kw in self.jobs if kw.get("id") != job_id]

def fake_register(name, author, desc, version, repo):
    def deco(cls):
        cls._meta = (name, author, desc, version, repo)
        return cls
    return deco

def fake_command(name, **kw):
    def deco(fn):
        fn._cmd = name
        return fn
    return deco

filter_mod = types.SimpleNamespace(
    command=fake_command,
    event_message_type=lambda t: (lambda fn: (setattr(fn, "_evt_type", t), fn)[1]),
)
filter_mod.EventMessageType = types.SimpleNamespace(ALL="ALL", GROUP="GROUP", PRIVATE="PRIVATE")

astrbot_api = types.ModuleType("astrbot.api")
astrbot_api.logger = logger
astrbot_event = types.ModuleType("astrbot.api.event")
astrbot_event.filter = filter_mod
astrbot_event.AstrMessageEvent = FakeEvent
astrbot_event.MessageChain = FakeMessageChain
astrbot_star = types.ModuleType("astrbot.api.star")
astrbot_star.Context = FakeContext
astrbot_star.register = fake_register

class FakeStar:
    def __init__(self, context):
        self.context = context

astrbot_star.Star = FakeStar

# ---------- 桩：apscheduler ----------
apsched = types.ModuleType("apscheduler")
apsched_sched = types.ModuleType("apscheduler.schedulers")
apsched_asyncio = types.ModuleType("apscheduler.schedulers.asyncio")
apsched_asyncio.AsyncIOScheduler = FakeAsyncIOScheduler
apsched_trig = types.ModuleType("apscheduler.triggers")
apsched_cron = types.ModuleType("apscheduler.triggers.cron")
apsched_cron.CronTrigger = lambda **kw: ("cron", kw)
apsched_interval = types.ModuleType("apscheduler.triggers.interval")
apsched_interval.IntervalTrigger = lambda **kw: ("interval", kw)

# ---------- 桩：aiohttp（测试不实际发请求） ----------
class FakeClientError(Exception):
    pass
aiohttp_mod = types.ModuleType("aiohttp")
aiohttp_mod.ClientError = FakeClientError
aiohttp_mod.ClientSession = object

sys.modules.update({
    "aiohttp": aiohttp_mod,
    "astrbot": types.ModuleType("astrbot"),
    "astrbot.api": astrbot_api,
    "astrbot.api.event": astrbot_event,
    "astrbot.api.star": astrbot_star,
    "apscheduler": apsched,
    "apscheduler.schedulers": apsched_sched,
    "apscheduler.schedulers.asyncio": apsched_asyncio,
    "apscheduler.triggers": apsched_trig,
    "apscheduler.triggers.cron": apsched_cron,
    "apscheduler.triggers.interval": apsched_interval,
})

spec = importlib.util.spec_from_file_location("main", MAIN_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules["main"] = mod
spec.loader.exec_module(mod)

PASS = 0
FAIL = 0
def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}")

async def collect(agen):
    """AstrBot handler 是 async generator，须用 async for 消费。"""
    out = []
    async for item in agen:
        out.append(item)
    return out

async def _mock_render_none(missions):
    """T2I 不可用时的渲染桩。"""
    return None

# ---------- 1. 注册元信息 ----------
check("register 元信息完整", mod.DnaHelperPlugin._meta[0] == "astrbot_plugin_dna_helper" and mod.DnaHelperPlugin._meta[3] == "2.5.0")

# ---------- 2. 默认配置 ----------
cfg_path = mod.CONFIG_FILE
if os.path.exists(cfg_path):  # 清理历史残留，保证默认配置断言成立
    os.remove(cfg_path)
check("默认配置", mod.load_config() == mod.DEFAULT_CONFIG)

# ---------- 3. 配置归一化 ----------
with open(cfg_path, "w", encoding="utf-8") as f:
    json.dump({"enable_scheduled_push": "yes", "whitelist_targets": [123, "a", "a", None]}, f)
cfg = mod.load_config()
check("配置类型校验(bool)", cfg["enable_scheduled_push"] is True)
check("配置归一化(去重/转str)", cfg["whitelist_targets"] == ["123", "a", "None"])

# ---------- 4. 格式化（游戏「委托密圈」风格，无等级） ----------
rows = [["角色A", "角色B"], ["武器C"], ["魔之楔D", "E"]]
text = mod.DnaHelperPlugin._format_missions_message(rows)
lines = text.split("\n")
check("格式化-头部", text.startswith("【密函委托更新】"))
check("格式化-分类区块", "角色  当前开放" in text and "武器  当前开放" in text and "魔之楔  当前开放" in text)
check("格式化-玩法逐行", "角色A" in lines and "角色B" in lines and "武器C" in lines and "魔之楔D" in lines and "E" in lines)
check("格式化-不显示等级", "Lv" not in text)
check("格式化-区块分隔", text.count(mod.BLOCK_SEPARATOR) == 2)
check("格式化-行数不足返回None", mod.DnaHelperPlugin._format_missions_message([["a"]]) is None)
check("格式化-非列表返回None", mod.DnaHelperPlugin._format_missions_message(None) is None)
check("格式化-行格式异常None", mod.DnaHelperPlugin._format_missions_message(["abc", "def", "ghi"]) is None)
check("签名-行格式异常返回空", mod.DnaHelperPlugin._missions_signature(["abc", "def", "ghi"]) == "")

# ---------- 4.5 HTML 模板（供 T2I 渲染） ----------
h = mod.DnaHelperPlugin._build_missions_html([["角<A>", "B"], ["C"], ["D", "E"]], "2026-01-01 12:00:00")
check("HTML-含标题与分类", "密 函 委 托" in h and "角色" in h and "武器" in h and "魔之楔" in h)
check("HTML-玩法与转义", "角&lt;A&gt;" in h and "密 函 委 托" in h)
check("HTML-三列横排", 'class="board"' in h and h.count('class="col"') == 3)
check("HTML-时间戳", "2026-01-01 12:00:00" in h)
check("HTML-行数不足None", mod.DnaHelperPlugin._build_missions_html([["a"]], "t") is None)
check("HTML-行格式异常None", mod.DnaHelperPlugin._build_missions_html(["abc", "def", "ghi"], "t") is None)

# ---------- 4.7 重点标注（手绘圈） ----------
_hl_rows = [["探险/无尽", "调停", "追缉"], ["拆解"], ["避险"]]
_h_on = mod.DnaHelperPlugin._build_missions_html(
    _hl_rows, "t", hl_explore=True, hl_mediation=True, scribble_style="A"
)
check("标注-红圈探险/无尽", 'class="mode hl-scribble"' in _h_on and 'stroke="#d63a2c"' in _h_on)
check("标注-蓝圈调停", 'stroke="#3f6fb5"' in _h_on)
check("标注-未命中不圈", _h_on.count('class="mode hl-scribble"') == 2)
_h_off = mod.DnaHelperPlugin._build_missions_html(
    _hl_rows, "t", hl_explore=False, hl_mediation=False, scribble_style="A"
)
check("标注-双关全关无圈", '<div class="mode hl-scribble">' not in _h_off)
_h_part = mod.DnaHelperPlugin._build_missions_html(
    _hl_rows, "t", hl_explore=True, hl_mediation=False, scribble_style="A"
)
check("标注-红开蓝关只有红圈", 'stroke="#d63a2c"' in _h_part and 'stroke="#3f6fb5"' not in _h_part)
_h_b = mod.DnaHelperPlugin._build_missions_html(
    _hl_rows, "t", hl_explore=True, hl_mediation=True, scribble_style="B"
)
check("标注-B样式双段路径", _h_b.count("<path") == 4)  # 两个圈各含 2 段 path
_h_c = mod.DnaHelperPlugin._build_missions_html(
    _hl_rows, "t", hl_explore=True, hl_mediation=True, scribble_style="C"
)
check("标注-C样式单段路径", _h_c.count("<path") == 2)
_h_rand = mod.DnaHelperPlugin._build_missions_html(
    _hl_rows, "t", hl_explore=True, hl_mediation=True, scribble_style=None
)
check("标注-条目独立随机样式可渲染", '<div class="mode hl-scribble">' in _h_rand and "<path" in _h_rand)
check("标注-默认配置开关为真", mod.DEFAULT_CONFIG["enable_highlight_explore"] is True and mod.DEFAULT_CONFIG["enable_highlight_mediation"] is True)

# ---------- 4.6 底部空白裁剪 ----------
from PIL import Image, ImageDraw
import io as _io
_timg = Image.new("RGB", (60, 120), (5, 7, 15))  # 暗背景
ImageDraw.Draw(_timg).rectangle([10, 10, 50, 61], fill=(255, 217, 138))  # 亮内容底部落在扫描范围，下方留大块空白
_tbuf = _io.BytesIO()
_timg.save(_tbuf, format="PNG")
_trimmed = mod.DnaHelperPlugin._trim_bottom_blank(_tbuf.getvalue())
_t2 = Image.open(_io.BytesIO(_trimmed))
check("裁剪-底部空白被裁", _t2.height < 120 and _t2.height >= 58)
check("裁剪-内容保留", _t2.getpixel((30, 30)) == (255, 217, 138))
_tbuf2 = _io.BytesIO()
_timg.crop((0, 0, 60, 60)).save(_tbuf2, format="PNG")
check("裁剪-无空白不误裁", mod.DnaHelperPlugin._trim_bottom_blank(_tbuf2.getvalue()) == _tbuf2.getvalue())
check("裁剪-非法输入原样返回", mod.DnaHelperPlugin._trim_bottom_blank(b"not png") == b"not png")

# ---------- 5. 实例化 + 定时器 ----------
ctx = FakeContext()
plugin = mod.DnaHelperPlugin(ctx)
asyncio.run(plugin._start_scheduler())
check("定时器启动", plugin.scheduler.running is True)
check("定时任务已注册", len(plugin.scheduler.jobs) == 1 and plugin.scheduler.jobs[0][1]["id"] == "dna_mission_push")

asyncio.run(plugin.terminate())
check("terminate 停止定时器", plugin.scheduler.running is False)

# ---------- 6. 推送流程（打桩网络与发送） ----------
plugin2 = mod.DnaHelperPlugin(ctx)
plugin2.config["whitelist_targets"] = ["g1", "g2"]
async def fake_fetch():
    return [["A", "B"], ["C"], ["D", "E"]]
plugin2._fetch_missions_from_api = fake_fetch
# T2I 不可用 → 回退文本推送
plugin2._render_missions_image = _mock_render_none
asyncio.run(plugin2._push_missions_to_whitelist())
check("推送-两个目标", len(ctx.sent) == 2 and ctx.sent[0][0] == "g1" and ctx.sent[1][0] == "g2")
check("推送-内容为格式化文本", ctx.sent[0][1].text.startswith("【密函委托更新】"))

# T2I 可用 → 图片推送
ctx.sent.clear()
fake_png = b"\x89PNG-fake-image-data"
async def fake_render_png(missions):
    return fake_png
plugin2._render_missions_image = fake_render_png
plugin2.config["last_pushed_signature"] = ""  # 重置指纹，确保本次推送
asyncio.run(plugin2._push_missions_to_whitelist())
check("推送-T2I可用发图片",
      len(ctx.sent) == 2 and ctx.sent[0][1].image_base64 == base64.b64encode(fake_png).decode("ascii"))
plugin2._render_missions_image = _mock_render_none

# 空白名单不发送
ctx.sent.clear()
plugin2.config["whitelist_targets"] = []
asyncio.run(plugin2._push_missions_to_whitelist())
check("推送-空白名单跳过", len(ctx.sent) == 0)

# 拉取失败不发送
plugin2.config["whitelist_targets"] = ["g1"]
async def fake_fetch_none():
    return None
plugin2._fetch_missions_from_api = fake_fetch_none
asyncio.run(plugin2._push_missions_to_whitelist())
check("推送-拉取失败跳过", len(ctx.sent) == 0)

# ---------- 7. 指令：添加/移除/启用/禁用 ----------
plugin2.config["whitelist_targets"] = []
ev = FakeEvent(message_str="/dna_添加白名单", unified_msg_origin="QQ_BOT:GroupMessage:99")
res = asyncio.run(collect(plugin2.add_whitelist(ev)))
check("添加-默认当前群", "QQ_BOT:GroupMessage:99" in res[0] and any("99" in t for t in plugin2.config["whitelist_targets"]))

ev2 = FakeEvent(message_str="/dna_添加白名单 abc")
res = asyncio.run(collect(plugin2.add_whitelist(ev2)))
check("添加-指定目标", "abc" in plugin2.config["whitelist_targets"])

ev3 = FakeEvent(message_str="/dna_移除白名单 abc")
res = asyncio.run(collect(plugin2.remove_whitelist(ev3)))
check("移除-成功", "abc" not in plugin2.config["whitelist_targets"] and "已移除" in res[0])

ev4 = FakeEvent(message_str="/dna_禁用推送")
res = asyncio.run(collect(plugin2.disable_push(ev4)))
check("禁用-配置写入", plugin2.config["enable_scheduled_push"] is False and plugin2.scheduler.running is False)

ev5 = FakeEvent(message_str="/dna_启用推送")
res = asyncio.run(collect(plugin2.enable_push(ev5)))
check("启用-配置写入并启动", plugin2.config["enable_scheduled_push"] is True and plugin2.scheduler.running is True)

# ---------- 8. 状态与帮助 ----------
ev6 = FakeEvent(message_str="/dna_状态")
res = asyncio.run(collect(plugin2.status(ev6)))
check("状态-包含版本", "2.5.0" in res[0] and "1 个" in res[0])
ev7 = FakeEvent(message_str="/dna_帮助")
res = asyncio.run(collect(plugin2.help_cmd(ev7)))
check("帮助-含全部指令", all(cmd in res[0] for cmd in ["/dna_状态", "/dna_启用推送", "/dna_禁用推送", "/dna_测试信息"]))

# ---------- 8.5 测试信息指令 ----------
# 成功（T2I 不可用，文本回退）：返回格式化信息 + 变化附注，且不改指纹
plugin2.config["last_pushed_signature"] = "old-sig"
async def fake_fetch_for_test():
    return [["X", "Y"], ["Z"], ["W", "V"]]
plugin2._fetch_missions_from_api = fake_fetch_for_test
plugin2._render_missions_image = _mock_render_none
ev8 = FakeEvent(message_str="/dna_测试信息")
res = asyncio.run(collect(plugin2.test_fetch_info(ev8)))
res_lines = res[0].split("\n")
check("测试信息-返回内容", len(res) == 1 and "角色  当前开放" in res[0] and "X" in res_lines and "Y" in res_lines and "魔之楔  当前开放" in res[0])
check("测试信息-附注变化", "与上次推送不同" in res[0])
check("测试信息-不改指纹", plugin2.config["last_pushed_signature"] == "old-sig")

# 成功（T2I 可用）：向当前群发送图片 + 文本提示
ctx.sent.clear()
plugin2._render_missions_image = fake_render_png
res = asyncio.run(collect(plugin2.test_fetch_info(ev8)))
check("测试信息-T2I可用发图",
      len(ctx.sent) == 1 and ctx.sent[0][0] == ev8.unified_msg_origin
      and ctx.sent[0][1].image_base64 == base64.b64encode(fake_png).decode("ascii")
      and "已发送当前密函信息图片" in res[0])
plugin2._render_missions_image = _mock_render_none

# API 失败：返回错误提示
async def fake_fetch_none():
    return None
plugin2._fetch_missions_from_api = fake_fetch_none
res = asyncio.run(collect(plugin2.test_fetch_info(ev8)))
check("测试信息-失败提示", len(res) == 1 and "失败" in res[0])

# ---------- 9. 内容去重 + 轮询 ----------
plugin3 = mod.DnaHelperPlugin(FakeContext())
asyncio.run(plugin3._start_scheduler())  # 轮询逻辑依赖 scheduler.running
plugin3.config["whitelist_targets"] = ["g1"]
plugin3.config["last_pushed_signature"] = ""

class FetchStub:
    def __init__(self):
        self.rows = None
    async def __call__(self):
        return self.rows

fetch = FetchStub()
plugin3._fetch_missions_from_api = fetch
plugin3._render_missions_image = _mock_render_none  # 去重测试走文本路径
sends3 = []
async def fake_send3(origin, msg):
    sends3.append((origin, msg))
    return True
plugin3._send_message = fake_send3

# 9.1 首次运行（无指纹）→ 推送并记录指纹
fetch.rows = [["A", "B"], ["C"], ["D", "E"]]
asyncio.run(plugin3._push_missions_to_whitelist())
check("去重-首次推送并记指纹",
      len(sends3) == 1 and plugin3.config["last_pushed_signature"] == "角色：A B\n武器：C\n魔之楔：D E")

# 9.2 内容相同 → 不推送 + 开启轮询
sends3.clear()
asyncio.run(plugin3._push_missions_to_whitelist())
check("去重-相同内容不推送", len(sends3) == 0)
check("去重-开启轮询任务", plugin3.scheduler.get_job(mod.POLL_JOB_ID) is not None)

# 9.3 轮询：内容仍相同 → 不推送、轮询保持
asyncio.run(plugin3._poll_missions())
check("轮询-内容相同不推送", len(sends3) == 0)
check("轮询-任务保持", plugin3.scheduler.get_job(mod.POLL_JOB_ID) is not None)

# 9.4 轮询：内容变化 → 推送 + 移除轮询 + 更新指纹
sends3.clear()
fetch.rows = [["A", "B"], ["C"], ["D", "NEW"]]
asyncio.run(plugin3._poll_missions())
check("轮询-发现新内容推送", len(sends3) == 1 and "魔之楔  当前开放" in sends3[0][1] and "NEW" in sends3[0][1].split("\n"))
check("轮询-移除轮询任务", plugin3.scheduler.get_job(mod.POLL_JOB_ID) is None)
check("轮询-指纹更新",
      plugin3.config["last_pushed_signature"] == "角色：A B\n武器：C\n魔之楔：D NEW")

# 9.5 轮询：API 失败 → 任务保持、不推送
sends3.clear()
fetch.rows = None
asyncio.run(plugin3._ensure_polling())
asyncio.run(plugin3._poll_missions())
check("轮询-API失败保持且不推送",
      plugin3.scheduler.get_job(mod.POLL_JOB_ID) is not None and len(sends3) == 0)

# 9.5.1 轮询幂等：已存在时不重建、不重置计时
n_before = len([j for j in plugin3.scheduler.jobs if j[1].get("id") == mod.POLL_JOB_ID])
asyncio.run(plugin3._ensure_polling())
n_after = len([j for j in plugin3.scheduler.jobs if j[1].get("id") == mod.POLL_JOB_ID])
check("轮询幂等-已存在不重建", n_before == 1 and n_after == 1)

# 9.6 定时任务：API 失败 → 开启轮询、不推送
asyncio.run(plugin3._push_missions_to_whitelist())
check("定时-API失败开启轮询且不推送",
      plugin3.scheduler.get_job(mod.POLL_JOB_ID) is not None and len(sends3) == 0)

# 9.7 全部发送失败 → 不记录指纹、保持轮询
fetch.rows = [["X", "Y"], ["Z"], ["W", "V"]]
async def fake_send_fail(origin, msg):
    return False
plugin3._send_message = fake_send_fail
asyncio.run(plugin3._poll_missions())
check("轮询-发送失败不记指纹",
      plugin3.config["last_pushed_signature"] == "角色：A B\n武器：C\n魔之楔：D NEW")
check("轮询-发送失败保持轮询", plugin3.scheduler.get_job(mod.POLL_JOB_ID) is not None)

# ---------- 10. 官方可视化配置（_conf_schema 框架配置） ----------
# 10.1 旧插件目录 config.json 有数据时，框架配置为空 → 自动迁移
with open(cfg_path, "w", encoding="utf-8") as f:
    json.dump({"enable_scheduled_push": True,
               "whitelist_targets": ["legacy:GroupMessage:888"],
               "last_pushed_signature": "旧指纹"}, f)

class FakeFrameworkConfig(dict):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.saved = 0
    def save_config(self):
        self.saved += 1

fw = FakeFrameworkConfig({"enable_scheduled_push": True, "whitelist_targets": [], "last_pushed_signature": ""})
plugin4 = mod.DnaHelperPlugin(FakeContext(), fw)
check("框架配置-迁移白名单", plugin4.config["whitelist_targets"] == ["legacy:GroupMessage:888"])
check("框架配置-迁移指纹", plugin4.config["last_pushed_signature"] == "旧指纹")
check("框架配置-迁移已保存", fw.saved == 1 and fw["whitelist_targets"] == ["legacy:GroupMessage:888"])
check("框架配置-迁移后旧文件重置",
      mod.load_config()["whitelist_targets"] == [] and mod.load_config()["last_pushed_signature"] == "")

# 10.2 修改配置后 _save_config 写回框架配置
plugin4.config["whitelist_targets"] = ["a", "b"]
plugin4._save_config()
check("框架配置-保存写回", fw["whitelist_targets"] == ["a", "b"] and fw.saved == 2)

# 10.3 框架配置已有数据时不覆盖迁移
fw2 = FakeFrameworkConfig({"enable_scheduled_push": False, "whitelist_targets": ["keep"], "last_pushed_signature": ""})
plugin5 = mod.DnaHelperPlugin(FakeContext(), fw2)
check("框架配置-已有数据不迁移", plugin5.config["whitelist_targets"] == ["keep"] and fw2.saved == 0)

# 10.4 无框架配置（旧环境/本地）回退插件目录 config.json
with open(cfg_path, "w", encoding="utf-8") as f:
    json.dump({"enable_scheduled_push": True,
               "whitelist_targets": ["legacy:GroupMessage:888"],
               "last_pushed_signature": ""}, f)
plugin6 = mod.DnaHelperPlugin(FakeContext())
check("框架配置-无框架回退本地", plugin6.config.get("whitelist_targets") == ["legacy:GroupMessage:888"])

# 10.5 _conf_schema.json 文件内容合法（官方 Schema 可被 json 解析且含必需字段）
with open(os.path.join(WORK_DIR, "_conf_schema.json"), encoding="utf-8") as f:
    schema = json.load(f)
check("框架配置-Schema 合法",
      isinstance(schema.get("whitelist_targets"), dict) and schema["whitelist_targets"].get("type") == "list"
      and schema["enable_scheduled_push"].get("type") == "bool")

# ---------- 11. 白名单智能适配（纯群号自动补全前缀） ----------
plugin7 = mod.DnaHelperPlugin(FakeContext())
plugin7.config["whitelist_targets"] = ["QQ_BOT:GroupMessage:111", "222"]
# 11.1 前缀缓存学习：收到一条消息后记录前缀
asyncio.run(plugin7._learn_prefix(FakeEvent(unified_msg_origin="QQ_BOT:PrivateMessage:999")))
check("智能适配-事件学习前缀", plugin7._last_prefix == "QQ_BOT")
# 11.2 纯群号补全
check("智能适配-纯群号补全", plugin7._resolve_target("222") == "QQ_BOT:GroupMessage:222")
# 11.3 完整格式原样返回
check("智能适配-完整格式原样", plugin7._resolve_target("QQ_BOT:GroupMessage:111") == "QQ_BOT:GroupMessage:111")
# 11.4 无缓存时从白名单已有完整条目推断前缀
plugin8 = mod.DnaHelperPlugin(FakeContext())
plugin8.config["whitelist_targets"] = ["QQ_BOT:GroupMessage:111"]
plugin8._last_prefix = None
check("智能适配-从已有条目推断", plugin8._resolve_target("222") == "QQ_BOT:GroupMessage:222")
# 11.5 完全无前缀信息：原样返回不崩溃
plugin9 = mod.DnaHelperPlugin(FakeContext())
plugin9.config["whitelist_targets"] = []
check("智能适配-无前缀信息原样", plugin9._resolve_target("222") == "222")
# 11.6 目标等价判断（完整 vs 群号）
check("智能适配-等价判断", plugin7._targets_equal("QQ_BOT:GroupMessage:222", "222") is True
      and plugin7._targets_equal("QQ_BOT:GroupMessage:222", "333") is False)
# 11.7 移除指令：用群号移除完整格式条目
plugin7.config["whitelist_targets"] = ["QQ_BOT:GroupMessage:111", "222"]
async def remove_via_group_number():
    gen = plugin7.remove_whitelist(FakeEvent("dna_移除白名单 111", "QQ_BOT:GroupMessage:111"))
    async for r in gen:
        pass
asyncio.run(remove_via_group_number())
check("智能适配-群号移除完整条目", plugin7.config["whitelist_targets"] == ["222"])
# 11.8 显示指令：完整格式显示为群号
async def show_via_group():
    gen = plugin7.show_whitelist(FakeEvent("dna_查看推送群", "QQ_BOT:GroupMessage:111"))
    out = []
    async for r in gen:
        out.append(r)
    return out
shown = asyncio.run(show_via_group())
check("智能适配-显示为群号", "222" in shown[0] and "GroupMessage" not in shown[0])

# ---------- 12. 图片开关 + 文字生图 API 配置 ----------
plugin10 = mod.DnaHelperPlugin(FakeContext())
plugin10.config["whitelist_targets"] = ["g1"]
# 12.1 图片推送关闭 → 跳过渲染，直接文本
render_called = {"n": 0}
async def spy_render(missions):
    render_called["n"] += 1
    return b"\x89PNG-fake"
plugin10._render_missions_image = spy_render
plugin10.config["enable_image_push"] = False
plugin10.config["last_pushed_signature"] = ""
plugin10._fetch_missions_from_api = fake_fetch
ctx10_sent = []
async def fake_send10(origin, msg):
    ctx10_sent.append((origin, msg))
    return True
plugin10._send_message = fake_send10
plugin10._send_image_message = lambda origin, png: False
asyncio.run(plugin10._push_missions_to_whitelist())
check("图片开关-关闭跳过渲染", render_called["n"] == 0 and len(ctx10_sent) == 1
      and ctx10_sent[0][1].startswith("【密函委托更新】"))
# 12.2 图片开关开启 + 渲染失败 → 文本回退（渲染被调用）
render_called["n"] = 0
plugin10.config["enable_image_push"] = True
plugin10._render_missions_image = _mock_render_none
plugin10.config["last_pushed_signature"] = ""
ctx10_sent.clear()
asyncio.run(plugin10._push_missions_to_whitelist())
check("图片开关-渲染失败回退文本", render_called["n"] == 0 and len(ctx10_sent) == 1)
# 12.3 文字生图 API 地址：留空回退官方默认
plugin10.config["t2i_api_url"] = ""
check("图片API-留空用默认", plugin10._t2i_url() == mod.T2I_URL)
# 12.4 文字生图 API 地址：自填生效（含首尾空白清洗）
plugin10.config["t2i_api_url"] = "  http://127.0.0.1:9999/generate  "
check("图片API-自填生效", plugin10._t2i_url() == "http://127.0.0.1:9999/generate")
# 12.5 配置归一化：非法类型回退默认
with open(cfg_path, "w", encoding="utf-8") as f:
    json.dump({"enable_image_push": "yes", "t2i_api_url": 123}, f)
cfg12 = mod.load_config()
check("图片配置-归一化", cfg12["enable_image_push"] is True and cfg12["t2i_api_url"] == "")

# ---------- 12.6 测试信息指令遵循图片开关：关闭时不出图 ----------
plugin11 = mod.DnaHelperPlugin(FakeContext())
plugin11.config["enable_image_push"] = False
plugin11.config["last_pushed_signature"] = "old-sig"
plugin11._fetch_missions_from_api = fake_fetch_for_test
render_called["n"] = 0
plugin11._render_missions_image = spy_render
ev11 = FakeEvent(message_str="/dna_测试信息")
res11 = asyncio.run(collect(plugin11.test_fetch_info(ev11)))
check("测试信息-图片开关关闭不出图", render_called["n"] == 0
      and len(res11) == 1 and "角色" in res11[0] and "与上次推送不同" in res11[0])

# ---------- 清理 ----------
if os.path.exists(cfg_path):
    os.remove(cfg_path)

print(f"\n结果: PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
