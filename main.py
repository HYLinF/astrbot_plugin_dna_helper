"""二重螺旋（DNA）密函委托定时推送插件 —— 兼容入口。

本文件仅为 AstrBot 加载入口，按功能拆分后的真实实现位于：
- dna_api.py            官方 dnabbs-api 客户端封装 + 1.3.x 签名工具（RSA/XOR/MD5 混淆）
- dna_login_server.py   登录页 HTTP 服务 + 验证码反代（aiohttp.web 独立监听）
- dna_plugin.py         主插件类 DnaHelperPlugin + 配置读写 + 定时推送/命令

符号在这里统一转发，保持 `import main` 的既有调用（含本地测试脚本）不变。
"""

import os
import sys

# AstrBot 按文件路径加载本入口，插件目录不保证在 sys.path 中；
# 先挂载插件目录，保证下面的 from dna_api / from dna_login_server / from dna_plugin 可解析。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 官方直连 API 模块
from dna_api import (
    DNA_API_BASE,
    DNA_API_TIMEOUT,
    DNA_ANDROID_SOURCE,
    DNA_ANDROID_UA,
    DNA_ANDROID_VERSION,
    DNA_FORM_URLENCODED,
    DNA_GAME_ID,
    DNA_PATH_LOGIN,
    DNA_PATH_REFRESH,
    DNA_PATH_ROLE_FOR_TOOL,
    DNA_PATH_RSA_KEY,
    DNA_PATH_SMS,
    DNA_RSA_FALLBACK_KEY,
    DNAOfficialAPI,
    REQUIRED_MISSION_ROWS,
    _build_sa_header,
    _gen_signed_headers,
    _md5_shuffle,
    _rand_digit_str,
    _rand_str,
    _rsa_encrypt,
    _sign_shuffled,
    _xor_encode,
)

# 登录页服务模块
from dna_login_server import (
    DNA_ANDROID_UA_ALICAP,
    DNA_CAPTCHA_HOSTS,
    DNA_CAPTCHA_ID,
    DNA_CAPTCHA_ORIGIN,
    DNA_CAPTCHA_REDIRECT,
    DNA_CAPTCHA_REFERER,
    DNA_CT4_URL,
    DNA_LOGIN_SESSION_TTL,
    DNA_SW_JS,
    DNALoginServer,
    LOGIN_PAGE_HTML,
)

# 主插件模块
from dna_plugin import (
    API_RETRY_DELAY,
    API_RETRY_TIMES,
    BLOCK_SEPARATOR,
    CONFIG_FILE,
    DEFAULT_CONFIG,
    DNA_ACCOUNT_INVALID,
    DNA_ACCOUNT_OK,
    HL_COLORS,
    HL_EXPLORE_NAMES,
    HL_MEDIATION_NAMES,
    MISSION_CATEGORIES,
    MISSIONS_API_QUERY,
    MISSIONS_API_TIMEOUT,
    MISSIONS_API_URL,
    PLUGIN_DESCRIPTION,
    PLUGIN_DIR,
    PLUGIN_NAME,
    PLUGIN_REPO,
    PLUGIN_VERSION,
    POLL_INTERVAL_MINUTES,
    POLL_JOB_ID,
    PUSH_CRON_MINUTE,
    PUSH_CRON_SECOND,
    PUSH_JOB_ID,
    PUSH_MISFIRE_GRACE_SECONDS,
    PUSH_TARGET_INTERVAL_SECONDS,
    SCRIBBLE_DEFAULT_WIDTH,
    SCRIBBLE_STYLES,
    STATUS_LABEL,
    T2I_CLIP,
    T2I_HTML_FONT,
    T2I_SCALE_LEVEL,
    T2I_TIMEOUT,
    T2I_URL,
    TEST_TARGET_INTERVAL_SECONDS,
    DnaHelperPlugin,
    _normalize_config,
    load_config,
    save_config,
)

# AstrBot 兼容修复：star_manager 加载插件时用 `path in star_map` 判断是否走
# __init_subclass__ 注册分支（star_map 的 key 是插件主类的 cls.__module__）。
# 单文件时代主类定义在入口模块，key 恰好等于 path，命中后会完成 handler 实例绑定；
# 拆分后主类定义在子模块 dna_plugin（__module__ = "dna_plugin"），与入口模块路径
# 不匹配会落入 legacy 加载分支且不做实例绑定，命令以未绑定函数被调用
# （TypeError: ... missing 'event'）。
# 这里在全部 handler 注册完成后，把 star_map 中的注册项补一个入口模块路径别名，
# 使 `path in star_map` 命中并走回正常的实例绑定分支。
import dna_plugin as _dna_plugin_module

_fix_astrbot_plugin_registration = getattr(
    _dna_plugin_module, "_fix_astrbot_plugin_registration", None
)
if _fix_astrbot_plugin_registration is not None:
    _fix_astrbot_plugin_registration(__name__)
