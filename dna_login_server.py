"""登录页 HTTP 服务（aiohttp.web 独立监听）。

默认绑定 127.0.0.1:8899（仅本机可访问）。展示给用户的公开地址由配置
dna_login_public_url 决定（用户自行填写公网地址并放行端口）。

验证码反代：前端加载官方 ct4.js SDK 时，把发往 *.alicaptcha.com 的请求
改写为同源 /dna_login/alicap/{auth}/{host}/{path}，由服务端注入 Android
UA 画像后转发白名单三主机（248 修复，参考狩月终端方案）。
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from typing import TYPE_CHECKING, Any

import httpx
from aiohttp import web

from astrbot.api import logger

from dna_api import DNAOfficialAPI

if TYPE_CHECKING:
    from dna_plugin import DnaHelperPlugin

# ------------------------- 登录服务常量 -------------------------
# 登录会话有效期（秒）
DNA_LOGIN_SESSION_TTL = 600
# 腾讯验证码（aliCaptcha）环节：官方 getSmsCode 缺 vJson 一律 500，必须走验证码。
DNA_CAPTCHA_ID = "114d4e96cc4536050c7efaeb7e4f3c8c"
DNA_CT4_URL = "https://dnabbs.yingxiong.com/lib/ct4.js"
# 验证码上游白名单（仅这三台主机可转发，不做开放代理）
DNA_CAPTCHA_HOSTS = frozenset(
    {"captcha.alicaptcha.com", "captchabak.alicaptcha.com", "static.alicaptcha.com"}
)
DNA_CAPTCHA_REDIRECT = frozenset({301, 302, 303, 307, 308})
# Windows 浏览器直连 alicaptcha 会因 UA 平台标识被上游拒绝（248），
# 反代把 UA/UA-CH 固定为官方 App 的 Android 画像后转发。
DNA_ANDROID_UA_ALICAP = (
    "Mozilla/5.0 (Linux; Android 12; V2304A) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)
DNA_CAPTCHA_REFERER = "https://dnabbs.yingxiong.com/"
DNA_CAPTCHA_ORIGIN = "https://dnabbs.yingxiong.com"

# ================= 登录页 HTML 模板 =================
LOGIN_PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>二重螺旋 密函助手 · 账号登录</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:100%}
body{min-height:100vh;display:flex;align-items:center;justify-content:center;background:radial-gradient(1200px 600px at 50% -10%,#1c2f5e 0%,#0d1220 55%,#070a12 100%);font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;color:#e8ecf5}
.card{width:min(92vw,400px);max-width:94vw;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.12);border-radius:16px;padding:30px 26px}
h1{font-size:18px;font-weight:600;letter-spacing:1px;margin-bottom:6px}
.sub{font-size:12px;color:#8b93a7;margin-bottom:24px}
.field{margin-bottom:16px}
label{display:block;font-size:12px;color:#aeb6c8;margin-bottom:6px}
input{width:100%;min-width:0;height:42px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.16);border-radius:10px;color:#e8ecf5;font-size:15px;padding:0 14px;outline:none}
input:focus{border-color:#5b8cff}
.row{display:flex;gap:10px;align-items:center;flex-wrap:nowrap}
.row>div{flex:1 1 auto;min-width:0}
.row input{width:100%}
button{height:42px;border:none;border-radius:10px;font-size:15px;cursor:pointer;transition:.15s}
#sms{flex:0 0 auto;width:118px;align-self:flex-end;background:rgba(91,140,255,.15);color:#7fa8ff;border:1px solid rgba(91,140,255,.4)}
#sms:disabled{color:#5c6478;background:rgba(255,255,255,.04);border-color:rgba(255,255,255,.1);cursor:default}
#login{width:100%;margin-top:6px;background:linear-gradient(135deg,#3d6bff,#5b8cff);color:#fff;font-weight:600}
#login:disabled{opacity:.5;cursor:default}
.hint{font-size:11px;color:#7b8295;line-height:1.7;margin-top:18px;word-break:break-word}
.tip{text-align:center;font-size:14px;color:#f0a8a8;line-height:1.8;padding:40px 0}
.ok{color:#9fd0a0}
</style>
</head>
<body>
<div class="card">
<h1>二重螺旋 · 密函助手</h1>
<div class="sub">绑定游戏账号，用于定时查询密函委托推送</div>
<div class="field"><label>手机号</label><input id="mobile" maxlength="11" inputmode="numeric" placeholder="游戏账号绑定的手机号"></div>
<div class="field row"><div><label>短信验证码</label><input id="code" maxlength="6" inputmode="numeric" placeholder="6 位数字"></div><button id="sms" type="button">获取验证码</button></div>
<button id="login" type="button">登 录</button>
<div class="hint" id="hint">登录即代表同意将账号凭据保存在机器人服务器上，用于定时查询密函。<br>凭据权限等同账号本人，请仅绑定本人账号。</div>
</div>
<script>
var AUTH = "__AUTH__";
var CAPTCHA_ID = "114d4e96cc4536050c7efaeb7e4f3c8c";
// ===== 验证码请求反代（248 修复，参考狩月终端方案）=====
// Windows 浏览器直连 alicaptcha 会因 UA 平台标识被上游拒绝；浏览器禁止 JS 改 UA，
// 因此把发往 *.alicaptcha.com 的请求改写到同源 /dna_login/alicap/ 反代，
// 由服务端注入 Android 画像后转发。hook 必须在 ct4.js 加载前生效（本块同步安装）。
var ALICAP_HOSTS = ["captcha.alicaptcha.com","captchabak.alicaptcha.com","static.alicaptcha.com"];
var ALICAP_PREFIX = "/dna_login/alicap/" + AUTH + "/";
function isAlicap(u){if(typeof u!=="string"||!u)return false;try{return ALICAP_HOSTS.indexOf(new URL(u,location.href).hostname)!==-1}catch(e){return false}}
function rewriteAlicap(u){var url=new URL(u,location.href);return ALICAP_PREFIX+url.hostname+url.pathname+url.search}
var _nf=window.fetch;
if(_nf){window.fetch=function(input,init){var u=typeof input==="string"?input:(input&&input.url);if(typeof u==="string"&&isAlicap(u)){input=typeof input==="string"?rewriteAlicap(u):new Request(rewriteAlicap(u),input)}return _nf.call(this,input,init)}}
var _xo=XMLHttpRequest.prototype.open;
XMLHttpRequest.prototype.open=function(){var args=Array.prototype.slice.call(arguments);if(typeof args[1]==="string"&&isAlicap(args[1])){args[1]=rewriteAlicap(args[1])}return _xo.apply(this,args)};
var _sd=Object.getOwnPropertyDescriptor(HTMLScriptElement.prototype,"src");
if(_sd&&_sd.set){Object.defineProperty(HTMLScriptElement.prototype,"src",{configurable:true,get:_sd.get,set:function(v){_sd.set.call(this,isAlicap(v)?rewriteAlicap(v):v)}})}
var _id=Object.getOwnPropertyDescriptor(HTMLImageElement.prototype,"src");
if(_id&&_id.set){Object.defineProperty(HTMLImageElement.prototype,"src",{configurable:true,get:_id.get,set:function(v){_id.set.call(this,isAlicap(v)?rewriteAlicap(v):v)}})}
if(navigator.sendBeacon){var _sb=navigator.sendBeacon.bind(navigator);navigator.sendBeacon=function(u,d){return _sb(isAlicap(u)?rewriteAlicap(u):u,d)}}
// Service Worker（仅 https/localhost 可用；不可用时由上面 hook 兜底）
if("serviceWorker" in navigator){navigator.serviceWorker.register("/dna_login/sw.js").catch(function(){})}
// ===== 登录逻辑 =====
var $=function(id){return document.getElementById(id)};
var smsBtn=$("sms"),loginBtn=$("login"),hint=$("hint");
var captchaPromise=null;
function setHint(t,ok){hint.className="hint"+(ok?" ok":"");hint.textContent=t}
function startCooldown(){var left=60;smsBtn.disabled=true;var t=setInterval(function(){smsBtn.textContent=left+"s";if(--left<=0){clearInterval(t);smsBtn.disabled=false;smsBtn.textContent="获取验证码"}},1000)}
// 按需加载官方验证码 SDK（ct4.js 直连官方域），clientType=android 是 248 修复关键
function loadCaptcha(){
  if(captchaPromise)return captchaPromise;
  captchaPromise=new Promise(function(resolve,reject){
    var script=document.createElement("script");var settled=false;
    function fail(){if(settled)return;settled=true;if(script.parentNode)script.parentNode.removeChild(script);reject(new Error("captcha unavailable"))}
    function init(){
      if(settled)return;
      if(typeof window.initAlicom4!=="function"){fail();return}
      try{
        window.initAlicom4({
          captchaId:CAPTCHA_ID,https:true,product:"bind",clientType:"android",
          clientVersion:"1.8.4",outside:true,rem:0.85,
          mi:{packageName:"com.dnabridge.dna_bridge",displayName:"DNA登录桥",
              appVer:"0.1.0",build:"1",clientVersion:"1.8.4",
              geeid:{bd:"$unknown",d:"$unknown",e:"$unknown",ver:"1.0.0",client_type:"android"}}
        },function(captcha){
          if(settled)return;
          captcha.onReady(function(){if(settled)return;settled=true;resolve(captcha)})
            .onSuccess(function(){
              var v=captcha.getValidate();
              if(v){requestSmsCode(JSON.stringify(Object.assign({captcha_id:CAPTCHA_ID},v)))}
              else{setHint("请完成安全验证",false)}
            })
            .onFail(function(){setHint("验证未通过，请重试",false)})
            .onError(function(){if(!settled)fail();setHint("安全验证失败，请稍后重试",false)});
        });
      }catch(e){fail()}
    }
    if(typeof window.initAlicom4==="function"){init()}
    else{
      script.src="https://dnabbs.yingxiong.com/lib/ct4.js";
      script.async=true;script.onload=init;script.onerror=fail;
      document.head.appendChild(script);
    }
  }).catch(function(e){captchaPromise=null;throw e});
  return captchaPromise;
}
function requestSmsCode(vJson){
  var m=$("mobile").value.trim();
  smsBtn.disabled=true;setHint("验证码发送中…",true);
  fetch("/dna_login/sms",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({auth:AUTH,mobile:m,vJson:vJson})})
  .then(function(r){return r.json()}).then(function(d){
    setHint(d.msg||(d.success?"验证码已发送，请查收短信":"验证码发送失败，请重试"),d.success);
    if(d.success){startCooldown()}
    else{smsBtn.disabled=false;smsBtn.textContent="获取验证码"}
  }).catch(function(){setHint("网络错误，请重试",false);smsBtn.disabled=false;smsBtn.textContent="获取验证码"});
}
smsBtn.onclick=function(){
  var m=$("mobile").value.trim();
  if(!/^1\\d{10}$/.test(m)){setHint("请输入正确的 11 位手机号",false);return}
  smsBtn.disabled=true;smsBtn.textContent="加载验证…";setHint("正在进行安全验证…",true);
  loadCaptcha().then(function(captcha){
    smsBtn.textContent="获取验证码";
    captcha.showCaptcha();
  }).catch(function(){setHint("安全验证未能加载，请检查网络后重试",false);smsBtn.disabled=false;smsBtn.textContent="获取验证码"});
};
loginBtn.onclick=function(){
  var m=$("mobile").value.trim(),c=$("code").value.trim();
  if(!/^1\\d{10}$/.test(m)){setHint("请输入正确的 11 位手机号",false);return}
  if(!/^\\d{4,8}$/.test(c)){setHint("请输入短信验证码",false);return}
  loginBtn.disabled=true;setHint("登录中…",true);
  fetch("/dna_login/submit",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({auth:AUTH,mobile:m,code:c})})
  .then(function(r){return r.json()}).then(function(d){
    setHint(d.msg,d.success);
    if(d.success){loginBtn.textContent="✓ 登录成功，可以关闭此页面";}
    else{loginBtn.disabled=false}
  }).catch(function(){setHint("网络错误，请重试",false);loginBtn.disabled=false});
};
</script>
</body>
</html>
"""

# Service Worker：把页面发往 *.alicaptcha.com 的请求改写为插件同源反代。
# 与页面内 hook 同一套白名单与反代前缀，会话 auth 取自受控登录页 URL。
DNA_SW_JS = """'use strict';
var ALICAP_HOSTS=['captcha.alicaptcha.com','captchabak.alicaptcha.com','static.alicaptcha.com'];
var LOGIN_PAGE_RE=/\\/dna_login\\/([^/?#]+)/;
self.addEventListener('install',function(){self.skipWaiting()});
self.addEventListener('activate',function(e){e.waitUntil(self.clients.claim())});
function sessionFromUrl(raw){if(typeof raw!=='string')return null;var m=LOGIN_PAGE_RE.exec(raw);if(!m)return null;return{auth:m[1],base:raw.slice(0,m.index)}}
async function resolveSession(e){if(!e.clientId)return null;var c=await self.clients.get(e.clientId);return c?sessionFromUrl(c.url):null}
self.addEventListener('fetch',function(e){
  var url;try{url=new URL(e.request.url)}catch(err){return}
  if(ALICAP_HOSTS.indexOf(url.hostname)===-1)return;
  e.respondWith((async function(){
    var s;try{s=await resolveSession(e)}catch(err){s=null}
    if(!s)return new Response('captcha proxy requires an active login session',{status:403});
    var target=s.base+'/alicap/'+s.auth+'/'+url.hostname+url.pathname+url.search;
    var h={};for(var k of ['accept','accept-language','content-type']){var v=e.request.headers.get(k);if(v)h[k]=v}
    var init={method:e.request.method,headers:h,redirect:'follow'};
    if(e.request.method!=='GET'&&e.request.method!=='HEAD'){init.body=await e.request.arrayBuffer()}
    try{return await fetch(target,init)}catch(err){return new Response('captcha proxy unavailable: '+err,{status:502})}
  })());
});
"""


class DNALoginServer:
    """登录页 HTTP 服务（aiohttp.web 独立监听）。

    默认绑定 127.0.0.1:8899（仅本机可访问）。展示给用户的公开地址由配置
    dna_login_public_url 决定（用户自行填写公网地址并放行端口）。
    """

    ROUTE_PREFIX = "/dna_login"

    def __init__(self, plugin: "DnaHelperPlugin") -> None:
        self._plugin = plugin
        self._runner: web.AppRunner | None = None
        self._sessions: dict[str, dict[str, Any]] = {}
        self._session_lock = asyncio.Lock()
        self._cleaner: asyncio.Task | None = None
        self._http: httpx.AsyncClient | None = None  # 验证码反代出站客户端（懒加载）

    @property
    def host(self) -> str:
        return str(self._plugin.config.get("dna_login_bind_host") or "127.0.0.1")

    @property
    def port(self) -> int:
        try:
            return int(self._plugin.config.get("dna_login_port") or 8899)
        except (TypeError, ValueError):
            return 8899

    @property
    def public_base(self) -> str:
        url = str(self._plugin.config.get("dna_login_public_url") or "").strip().rstrip("/")
        if url:
            return url
        return f"http://{self.host}:{self.port}"

    # ---------- 生命周期 ----------
    async def start(self) -> bool:
        if self._runner is not None:
            return True
        app = web.Application()
        app.router.add_get(f"{self.ROUTE_PREFIX}/{{auth}}", self._page)
        app.router.add_post(f"{self.ROUTE_PREFIX}/sms", self._sms)
        app.router.add_post(f"{self.ROUTE_PREFIX}/submit", self._submit)
        app.router.add_get(f"{self.ROUTE_PREFIX}/sw.js", self._sw)
        app.router.add_route(
            "*",
            f"{self.ROUTE_PREFIX}/alicap/{{auth}}/{{host}}/{{path:.*}}",
            self._alicap,
        )
        try:
            self._runner = web.AppRunner(app)
            await self._runner.setup()
            site = web.TCPSite(self._runner, self.host, self.port)
            await site.start()
        except Exception as e:
            logger.error(f"登录服务启动失败（{self.host}:{self.port}）: {e}")
            self._runner = None
            return False
        self._cleaner = asyncio.create_task(self._clean_loop())
        logger.info(f"登录服务已启动: {self.public_base}")
        return True

    async def stop(self) -> None:
        if self._cleaner is not None:
            self._cleaner.cancel()
            self._cleaner = None
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                pass
            self._runner = None
        self._sessions.clear()
        logger.info("登录服务已停止")

    async def _clean_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            now = time.time()
            stale = [
                a
                for a, s in self._sessions.items()
                if now - s.get("at", 0) > DNA_LOGIN_SESSION_TTL
            ]
            for a in stale:
                self._sessions.pop(a, None)

    # ---------- 会话 ----------
    async def create_session(self, actor: dict[str, str]) -> str:
        auth = secrets.token_urlsafe(24)
        async with self._session_lock:
            self._sessions[auth] = {
                "actor": actor,
                "at": time.time(),
                "mobile": "",
                "dev_code": "",
            }
        return auth

    def _session(self, auth: str) -> dict | None:
        s = self._sessions.get(auth)
        if s is None:
            return None
        if time.time() - s.get("at", 0) > DNA_LOGIN_SESSION_TTL:
            self._sessions.pop(auth, None)
            return None
        return s

    # ---------- 路由 ----------
    async def _sw(self, request: web.Request) -> web.Response:

        return web.Response(text=DNA_SW_JS, content_type="application/javascript")

    async def _http_client(self) -> httpx.AsyncClient:
        """懒加载验证码反代出站客户端：只限制建连，其余阶段不设截止。"""
        if self._http is None:
            import httpx

            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=None, write=None, pool=None),
                trust_env=False,
                follow_redirects=False,
            )
        return self._http

    def _rewrite_redirect_location(self, location: str, base_url: str, auth: str) -> str | None:
        """校验上游 Location 并重写为本地同源反代地址；不在白名单返回 None。"""
        try:
            import httpx

            target = httpx.URL(base_url).join(location)
        except Exception:
            return None
        host = (target.host or "").strip().lower()
        if target.scheme != "https" or host not in DNA_CAPTCHA_HOSTS:
            return None
        if target.port not in (None, 443):
            return None
        try:
            raw_path = target.raw_path.decode("ascii")
        except Exception:
            return None
        return f"{self.ROUTE_PREFIX}/alicap/{auth}/{host}{raw_path}"

    async def _alicap(self, request: web.Request) -> web.Response:
        """验证码反代：白名单三主机 + Android UA 画像转发，不做开放代理。

        路径 /dna_login/alicap/{auth}/{host}/{path}（host 与 path 保留原始编码，
        用 request.raw_path 解析；解码后的 match_info 会改写 %2F 等参数）。
        仅对有效会话转发；匿名/过期会话不产生任何上游流量。
        """

        raw_path = getattr(request, "raw_path", None) or request.path
        prefix = f"{self.ROUTE_PREFIX}/alicap/"
        if not raw_path.startswith(prefix):
            return web.Response(status=400, text="bad path")
        rest = raw_path[len(prefix):]
        auth, sep, remainder = rest.partition("/")
        if not sep or not auth:
            return web.Response(status=400, text="bad auth")
        host, sep, sub = remainder.partition("/")
        if not sep or not host:
            return web.Response(status=400, text="bad host")
        if self._session(auth) is None:
            return web.Response(status=403, text="session expired")
        hostname = host.strip().lower()
        if hostname not in DNA_CAPTCHA_HOSTS:
            return web.Response(status=403, text="host not allowed")
        url = f"https://{hostname}/{sub}"
        query = request.query_string
        if query and "?" not in sub:
            url += "?" + query
        logger.info(
            "验证码反代 req method=%s host=%s path=%s",
            request.method,
            hostname,
            sub[:120],
        )
        headers = {
            "User-Agent": DNA_ANDROID_UA_ALICAP,
            "sec-ch-ua-platform": '"Android"',
            "sec-ch-ua-mobile": "?1",
            "Accept": request.headers.get("Accept", "*/*"),
            "Accept-Language": request.headers.get("Accept-Language", "zh-CN,zh;q=0.9"),
            "Referer": DNA_CAPTCHA_REFERER,
            "Origin": DNA_CAPTCHA_ORIGIN,
        }
        method = request.method.upper()
        if method not in ("GET", "HEAD"):
            headers["Content-Type"] = request.headers.get(
                "Content-Type", "application/x-www-form-urlencoded"
            )
        body = await request.read() if method not in ("GET", "HEAD") else None
        try:
            client = await self._http_client()
            resp = await client.request(method, url, headers=headers, content=body or None)
        except Exception as e:
            logger.warning("验证码反代上游失败 kind=%s", type(e).__name__)
            return web.Response(status=502, text="captcha proxy upstream error")
        if resp.status_code in DNA_CAPTCHA_REDIRECT:
            location = resp.headers.get("Location")
            if location:
                new_loc = self._rewrite_redirect_location(location, url, auth)
                if new_loc:
                    return web.Response(status=resp.status_code, headers={"Location": new_loc})
            return web.Response(status=resp.status_code)
        logger.info("验证码反代 resp status=%s ct=%s", resp.status_code, resp.headers.get("Content-Type", "")[:40])
        if "verify" in sub:
            logger.info("验证码反代 verify body=%s", resp.content[:200])
        if sub.startswith("load"):
            logger.info("验证码反代 load query=%s", request.query_string[:400])
            logger.info("验证码反代 load body=%s", resp.content[:300])
        return web.Response(
            status=resp.status_code,
            body=resp.content,
            content_type=resp.headers.get("Content-Type", "application/octet-stream").split(";")[0],
        )

    async def _page(self, request: web.Request) -> web.Response:
        auth = request.match_info.get("auth", "")
        if self._session(auth) is None:
            html_body = LOGIN_PAGE_HTML.replace(
                "__AUTH__", ""
            ).replace(
                '<div class="field"><label>手机号</label>',
                '<div class="tip">登录已过期或链接无效，请重新在群里发送「dna_登录」</div><div class="field"><label>手机号</label>',
            )
            return self._html(html_body)
        return self._html(LOGIN_PAGE_HTML.replace("__AUTH__", auth))

    async def _sms(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return self._json({"success": False, "msg": "无效请求"})
        auth = str(body.get("auth") or "")
        mobile = str(body.get("mobile") or "").strip()
        v_json = str(body.get("vJson") or "")
        session = self._session(auth)
        if session is None:
            return self._json({"success": False, "msg": "登录已过期，请重新发起"})
        if not re.fullmatch(r"1\d{10}", mobile):
            return self._json({"success": False, "msg": "手机号格式不正确"})
        dev_code = session.get("dev_code") or DNAOfficialAPI.new_dev_code()
        api = DNAOfficialAPI()
        ok, sms_msg = await api.request_sms(mobile, v_json, dev_code)
        if not ok:
            msg = f"验证码发送失败：{sms_msg}"
            return self._json({"success": False, "msg": msg})
        session["mobile"] = mobile
        session["dev_code"] = dev_code
        return self._json({"success": True, "msg": "验证码已发送，请查收短信"})

    async def _submit(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return self._json({"success": False, "msg": "无效请求"})
        auth = str(body.get("auth") or "")
        mobile = str(body.get("mobile") or "").strip()
        code = str(body.get("code") or "").strip()
        session = self._session(auth)
        if session is None:
            return self._json({"success": False, "msg": "登录已过期，请重新发起"})
        if not session.get("mobile") or session.get("mobile") != mobile:
            return self._json({"success": False, "msg": "请先为当前手机号获取验证码"})
        if not re.fullmatch(r"\d{4,8}", code):
            return self._json({"success": False, "msg": "验证码格式不正确"})
        dev_code = session.get("dev_code") or DNAOfficialAPI.new_dev_code()
        api = DNAOfficialAPI()
        cred = await api.login(mobile, code, dev_code)
        if cred is None:
            return self._json({"success": False, "msg": "登录失败：手机号或验证码不正确"})
        actor = session.get("actor") or {}
        ok = self._plugin.bind_official_account(
            user_id=str(actor.get("user_id", "")),
            mobile=mobile,
            token=cred["token"],
            refresh_token=cred.get("refresh_token", ""),
            d_num=cred.get("d_num", ""),
            dev_code=dev_code,
        )
        if not ok:
            return self._json({"success": False, "msg": "凭据保存失败，请联系管理员查看日志"})
        self._sessions.pop(auth, None)
        return self._json({"success": True, "msg": "登录成功，凭据已绑定到你的账号"})

    # ---------- 响应 ----------
    @staticmethod
    def _json(payload: dict) -> web.Response:

        return web.json_response(payload)

    @staticmethod
    def _html(text: str) -> web.Response:

        return web.Response(text=text, content_type="text/html")


