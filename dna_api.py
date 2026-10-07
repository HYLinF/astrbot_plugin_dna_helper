"""官方 dnabbs-api 客户端封装（直连《二重螺旋》官方接口）。

包含官方 1.3.x 签名方案（RSA + XOR + MD5 混淆）、短信验证码 / 登录 / 续期 /
密函拉取。凭据（token / refreshToken / d_num / dev_code）由插件账号管理模块持有。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import random
import secrets
import time
from typing import Any

import aiohttp

from astrbot.api import logger

# ------------------------- 官方直连 API 常量 -------------------------
DNA_API_BASE = "https://dnabbs-api.yingxiong.com"
DNA_GAME_ID = 268
DNA_API_TIMEOUT = 15  # 秒
# 官方 Android 客户端基础头（1.3.x 签名方案）
DNA_ANDROID_VERSION = "1.3.2"
DNA_ANDROID_SOURCE = "android"
DNA_ANDROID_UA = "okhttp/3.10.0"
DNA_FORM_URLENCODED = "application/x-www-form-urlencoded; charset=utf-8"
# 官方接口路径
DNA_PATH_RSA_KEY = "/config/getRsaPublicKey"
DNA_PATH_SMS = "/user/getSmsCode"
DNA_PATH_LOGIN = "/user/sdkLogin"
DNA_PATH_REFRESH = "/user/refreshToken"
DNA_PATH_ROLE_FOR_TOOL = "/role/defaultRoleForTool"
# 获取 RSA 公钥失败时的回退公钥（与官方客户端内置一致）
DNA_RSA_FALLBACK_KEY = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDGpdbezK+eknQZQzPOjp8mr/dP+"
    "QHwk8CRkQh6C6qFnfLH3tiyl0pnt3dePuFDnM1PUXGhCkQ157ePJCQgkDU2+mimDmXh0oLFn9zuWSp+"
    "U8uLSLX3t3PpJ8TmNCROfUDWvzdbnShqg7JfDmnrOJz49qd234W84nrfTHbzdqeigQIDAQAB"
)
# 密函列表至少需要 3 行（角色 / 武器 / 魔之楔）
REQUIRED_MISSION_ROWS = 3

# ================= 官方 1.3.x 签名工具 =================
# ================= v2.7.0 官方直连：签名工具 =================
def _rand_digit_str(length: int) -> str:
    """官方 1.3.x 随机数字串（与 Java 端 p63.b() 字符集一致）。"""
    chars = "01234567890123456789012345678901234567890123456789010123456789"
    return "".join(random.choice(chars) for _ in range(length))


def _rand_str(length: int) -> str:
    """随机字母数字串（官方客户端 rk 生成方式）。"""
    chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(random.choice(chars) for _ in range(length))


def _rsa_encrypt(data: str, public_key_b64: str) -> str:
    """RSA/ECB/PKCS1Padding 加密（每段最多 117 字节），返回 base64。"""
    from Crypto.Cipher import PKCS1_v1_5
    from Crypto.PublicKey import RSA

    key = RSA.importKey(base64.b64decode(public_key_b64))
    cipher = PKCS1_v1_5.new(key)
    raw = data.encode("utf-8")
    max_block = 117
    result = b""
    offset = 0
    while offset < len(raw):
        block = raw[offset : offset + max_block]
        result += cipher.encrypt(block)
        offset += max_block
    return base64.b64encode(result).decode("utf-8")


def _xor_encode(text: str, key: str) -> str:
    """官方自定义 XOR 编码（字节值相加，非异或）。"""
    tb = text.encode("utf-8")
    kb = key.encode("utf-8")
    return "".join(f"@{(tb[i] & 255) + (kb[i % len(kb)] & 255)}" for i in range(len(tb)))


def _md5_shuffle(md5_hex: str) -> str:
    """MD5 结果位置混淆: 1↔13, 5↔17, 7↔23。"""
    if len(md5_hex) <= 23:
        return md5_hex
    chars = list(md5_hex)
    for i, j in [(1, 13), (5, 17), (7, 23)]:
        chars[i], chars[j] = chars[j], chars[i]
    return "".join(chars)


def _sign_shuffled(params: dict[str, Any], app_key: str) -> str:
    """按键排序拼接参数 → MD5 → 位置混淆。"""
    pairs = [
        f"{k}={params[k]}"
        for k in sorted(params)
        if params[k] is not None and str(params[k]) != ""
    ]
    pairs.append(app_key)
    md5_hash = hashlib.md5("&".join(pairs).encode("utf-8")).hexdigest().upper()
    return _md5_shuffle(md5_hash)


def _build_sa_header(raw_sa: str, timestamp_ms: int) -> str:
    """1.3.0 sa 头构建：30 位随机数 4 次位置交换 + 3 处插入时间戳 → 43 位。"""

    def _swap(text: str, i: int, j: int) -> str:
        if i < 0 or j < 0 or i >= len(text) or j >= len(text):
            return text
        chars = list(text)
        chars[i], chars[j] = chars[j], chars[i]
        return "".join(chars)

    sa = raw_sa
    for i, j in [(1, 17), (9, 20), (15, 16), (22, 27)]:
        sa = _swap(sa, i, j)
    ts = str(timestamp_ms)
    if len(sa) != 30 or len(ts) < 13:
        return sa
    time_idx = 0
    out = []
    for i in range(len(sa)):
        if i == 8 or i == 16:
            out.append(ts[time_idx : time_idx + 5])
            time_idx += 5
        elif i == 22:
            out.append(ts[time_idx : time_idx + 3])
            time_idx += 3
        out.append(sa[i])
    return "".join(out)


def _gen_signed_headers(
    base_headers: dict[str, str],
    payload: dict[str, Any],
    rsa_public_key: str,
) -> dict[str, str]:
    """为官方 1.3.x 请求生成 tn/sa 签名头。"""
    rk = _rand_str(16)
    raw_sa = _rand_digit_str(30)
    sa = _build_sa_header(raw_sa, int(time.time() * 1000))

    sign_params = {k: str(v) for k, v in payload.items()}
    if base_headers.get("token"):
        sign_params["token"] = base_headers["token"]
    sign_params["sa"] = raw_sa

    sign_encoded = _xor_encode(_sign_shuffled(sign_params, rk), rk)
    tn = f"{_rsa_encrypt(rk, rsa_public_key)},{sign_encoded}"

    out = dict(base_headers)
    out.update({"sa": sa, "tn": tn})
    return out


class DNAOfficialAPI:
    """官方 dnabbs-api 客户端封装：RSA 公钥、短信验证码、登录、续期、密函拉取。"""

    def __init__(self) -> None:
        self._rsa_key: str | None = None
        self._rsa_at: float = 0.0

    # ---------- 基础 ----------
    @staticmethod
    def _base_header(dev_code: str = "", token: str = "") -> dict[str, str]:
        header = {
            "version": DNA_ANDROID_VERSION,
            "source": DNA_ANDROID_SOURCE,
            "Content-Type": DNA_FORM_URLENCODED,
            "User-Agent": DNA_ANDROID_UA,
        }
        if dev_code:
            header["devCode"] = dev_code
        if token:
            header["token"] = token
        return header

    @staticmethod
    def new_dev_code() -> str:
        """生成设备码（官方客户端一致：'2' + 32 位 hex）。"""
        return "2" + secrets.token_hex(16)

    async def _rsa_public_key(self) -> str:
        if self._rsa_key and time.time() - self._rsa_at < 86400:
            return self._rsa_key
        key = DNA_RSA_FALLBACK_KEY
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    DNA_API_BASE + DNA_PATH_RSA_KEY,
                    headers=self._base_header(dev_code=self.new_dev_code()),
                    timeout=aiohttp.ClientTimeout(total=DNA_API_TIMEOUT),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if isinstance(data, dict) and data.get("success"):
                            got = (data.get("data") or {}).get("key")
                            if isinstance(got, str) and got:
                                key = got
        except Exception:
            pass
        self._rsa_key = key
        self._rsa_at = time.time()
        return key

    async def _post(
        self,
        path: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        rsa: bool = True,
    ) -> dict | None:
        """统一 POST：可选 RSA 签名 → 请求 → JSON 解析。"""
        if rsa:
            headers = _gen_signed_headers(headers, payload, await self._rsa_public_key())
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    DNA_API_BASE + path,
                    headers=headers,
                    data=payload,
                    timeout=aiohttp.ClientTimeout(total=DNA_API_TIMEOUT),
                ) as resp:
                    if resp.status != 200:
                        logger.warning(f"官方接口 {path} 返回 HTTP {resp.status}")
                        return None
                    try:
                        return await resp.json()
                    except Exception as e:
                        logger.warning(f"官方接口 {path} 响应解析失败: {e}")
                        return None
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            logger.warning(f"官方接口 {path} 请求失败: {e}")
            return None

    @staticmethod
    def _ok(data: dict | None) -> bool:
        return bool(
            data and data.get("success") and data.get("code") in (0, 200)
        )

    # ---------- 登录 ----------
    async def request_sms(self, mobile: str, v_json: str, dev_code: str) -> tuple[bool, str]:
        """请求短信验证码；vJson 为腾讯验证码结果（留空则尝试免验证码）。

        返回 (是否成功, 错误信息)。失败时尽量透传官方 msg 便于前端展示。
        """
        resp = await self._post(
            DNA_PATH_SMS,
            self._base_header(dev_code=dev_code),
            {"mobile": str(mobile), "vJson": v_json, "isCaptcha": 1},
            rsa=True,
        )
        logger.info("getSmsCode vjson=%s", v_json[:200])
        logger.info("getSmsCode resp code=%s msg=%s", resp.get("code") if isinstance(resp, dict) else type(resp).__name__, resp.get("msg", "")[:80] if isinstance(resp, dict) else "")
        if self._ok(resp):
            return True, ""
        msg = ""
        if isinstance(resp, dict):
            msg = str(resp.get("msg") or "").strip()
            code = resp.get("code")
            if code == 248 or "验证码" in msg:
                msg = msg or "需要完成安全验证"
        if not msg:
            msg = "手机号未注册或服务繁忙，请稍后重试"
        return False, msg

    async def login(self, mobile: str, code: str, dev_code: str) -> dict | None:
        """手机号+验证码登录，返回 {token, refresh_token, d_num} 或 None。"""
        resp = await self._post(
            DNA_PATH_LOGIN,
            self._base_header(dev_code=dev_code),
            {
                "code": str(code),
                "gameList": DNA_GAME_ID,
                "loginType": 1,
                "mobile": str(mobile),
            },
            rsa=True,
        )
        if not self._ok(resp):
            return None
        data = resp.get("data")
        if not isinstance(data, dict):
            return None
        token = data.get("token") or data.get("cookie") or ""
        refresh_token = data.get("refreshToken") or ""
        d_num = data.get("dNum") or data.get("dnum") or ""
        if not token:
            return None
        return {"token": token, "refresh_token": refresh_token, "d_num": d_num}

    async def refresh(self, token: str, refresh_token: str, dev_code: str) -> dict | None:
        """refreshToken 续期，返回 {token, d_num} 或 None。"""
        resp = await self._post(
            DNA_PATH_REFRESH,
            self._base_header(dev_code=dev_code, token=token),
            {"refreshToken": refresh_token},
            rsa=True,
        )
        if not self._ok(resp):
            return None
        data = resp.get("data")
        if not isinstance(data, dict):
            return None
        new_token = data.get("token") or ""
        d_num = data.get("dNum") or data.get("dnum") or ""
        if not new_token:
            return None
        return {"token": new_token, "d_num": d_num}

    # ---------- 密函 ----------
    async def fetch_missions(self, token: str, dev_code: str) -> list | None:
        """defaultRoleForTool(type=1) 拉当前密函；返回三行列表或 None。"""
        resp = await self._post(
            DNA_PATH_ROLE_FOR_TOOL,
            self._base_header(dev_code=dev_code, token=token),
            {"type": 1},
            rsa=True,
        )
        if not self._ok(resp):
            return None
        data = resp.get("data")
        instance_info = None
        if isinstance(data, dict):
            instance_info = data.get("instanceInfo")
        elif isinstance(data, list):
            instance_info = data
        if not isinstance(instance_info, list) or len(instance_info) < REQUIRED_MISSION_ROWS:
            return None
        rows = []
        for sec in instance_info[:REQUIRED_MISSION_ROWS]:
            if not isinstance(sec, dict):
                return None
            instances = sec.get("instances")
            if not isinstance(instances, list) or not instances:
                return None
            names = [
                item.get("name")
                for item in instances
                if isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and item["name"]
            ]
            if not names:
                return None
            rows.append(names)
        return rows


