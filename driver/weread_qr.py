"""
微信读书(Weread)扫码登录驱动

通过微信读书 Web API 实现扫码登录流程：
1. 获取登录 UID
2. 生成二维码
3. 轮询扫码状态
4. 登录成功后提取并保存 Cookie

参考: https://github.com/nasonliu/papers3-weread
"""

import os
import time
import json
import base64
from io import BytesIO
from threading import Lock, Timer
from typing import Optional, Dict, Any

import requests

from core.print import print_info, print_success, print_error, print_warning

WEREAD_BASE = "https://weread.qq.com"
WEREAD_API = "https://i.weread.qq.com"
QR_IMAGE_PATH = "static/weread_qrcode.png"


class WereadQRLogin:
    """微信读书扫码登录管理器（单例模式）"""

    _instance = None
    _lock = Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        self._uid: str = ""
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": "https://weread.qq.com",
            "Referer": "https://weread.qq.com/",
        })

        self._logged_in = False
        self._login_result: Dict[str, Any] = {}
        self._login_error: str = ""
        self._qr_code_path = QR_IMAGE_PATH
        self._has_qr = False

        # 用于轮询的定时器
        self._poll_timer: Optional[Timer] = None
        self._poll_interval = 2  # 轮询间隔（秒）
        self._poll_timeout = 300  # 最大轮询时间（秒）
        self._poll_start_time: float = 0

        # 确保目录存在
        os.makedirs(os.path.dirname(self._qr_code_path), exist_ok=True)

    # ---------- 对外接口 ----------

    def GetCode(self, CallBack=None) -> Optional[str]:
        """
        获取登录二维码
        返回二维码图片 URL 或 base64 数据
        """
        self._reset()

        # 第一步: 获取登录 UID
        uid = self._get_login_uid()
        if not uid:
            print_error("Weread QR: 获取登录 UID 失败")
            return None

        self._uid = uid
        print_info(f"Weread QR: 获取到 UID = {uid}")

        # 第二步: 生成二维码图片
        qr_url = self._generate_qr_image(uid)
        if not qr_url:
            return None

        self._has_qr = True

        # 第三步: 启动轮询
        if CallBack:
            self._start_polling(CallBack)

        return qr_url

    def QrStatus(self) -> Dict[str, Any]:
        """
        返回二维码/登录状态
        返回格式: { "login_status": bool, "code_url": str, "msg": str, "data": dict }
        """
        if not self._uid:
            return {"login_status": False, "code_url": "", "msg": "未开始获取二维码", "data": {}}

        if self._logged_in:
            return {
                "login_status": True,
                "code_url": self._qr_code_path,
                "msg": "登录成功",
                "data": self._login_result,
            }

        # 登录成功但 Cookie 验证失败
        if self._login_error:
            return {
                "login_status": False,
                "code_url": self._qr_code_path,
                "msg": self._login_error,
                "data": {"logicCode": "COOKIE_INVALID"},
            }

        # 检查是否有二维码图片
        if not self._has_qr:
            return {"login_status": False, "code_url": "", "msg": "二维码获取中...", "data": {}}

        # 检查是否超时
        if self._poll_start_time and (time.time() - self._poll_start_time > self._poll_timeout):
            msg = "二维码已过期，请重新获取"
            print_warning(f"Weread QR: {msg}")
            return {"login_status": False, "code_url": self._qr_code_path, "msg": msg, "data": {}}

        return {
            "login_status": False,
            "code_url": self._qr_code_path,
            "msg": "等待扫码...",
            "data": {},
        }

    def GetHasCode(self) -> bool:
        """二维码图片文件是否存在"""
        return os.path.exists(self._qr_code_path)

    def HasLogin(self) -> bool:
        """是否已登录成功"""
        return self._logged_in

    def GetLoginResult(self) -> Dict[str, Any]:
        """获取登录结果（Cookie 等）"""
        return self._login_result if self._logged_in else {}

    async def Close(self):
        """关闭登录流程并清理"""
        self._stop_polling()
        # 清理二维码图片
        if os.path.exists(self._qr_code_path):
            try:
                os.remove(self._qr_code_path)
            except Exception:
                pass
        return self._login_result if self._logged_in else {}

    # ---------- 内部方法 ----------

    def _reset(self):
        """重置登录状态"""
        self._uid = ""
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": "https://weread.qq.com",
            "Referer": "https://weread.qq.com/",
        })
        self._logged_in = False
        self._login_result = {}
        self._login_error = ""
        self._has_qr = False
        self._poll_start_time = 0
        self._stop_polling()

    def _get_login_uid(self) -> Optional[str]:
        """
        获取微信读书登录 UID
        GET https://weread.qq.com/api/auth/getLoginUid
        """
        try:
            url = f"{WEREAD_BASE}/api/auth/getLoginUid"
            resp = self._session.get(url, timeout=20)
            if resp.status_code != 200:
                print_error(f"Weread QR: getLoginUid 返回 {resp.status_code}")
                return None

            data = resp.json()
            uid = data.get("uid") or data.get("data", {}).get("uid")
            return str(uid) if uid else None
        except requests.exceptions.Timeout:
            print_error("Weread QR: getLoginUid 超时")
            return None
        except Exception as e:
            print_error(f"Weread QR: getLoginUid 异常: {e}")
            return None

    def _generate_qr_image(self, uid: str) -> Optional[str]:
        """
        根据 UID 生成微信读书登录二维码图片
        使用 qrcode 库生成 PNG 图片
        """
        confirm_url = f"{WEREAD_BASE}/web/confirm?uid={uid}"

        try:
            import qrcode

            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=10,
                border=4,
            )
            qr.add_data(confirm_url)
            qr.make(fit=True)

            img = qr.make_image(fill_color="black", back_color="white")
            img.save(self._qr_code_path, format="PNG")
            print_success(f"Weread QR: 二维码已保存到 {self._qr_code_path}")

            return self._qr_code_path
        except ImportError:
            # 没有 qrcode 库时，不再静默 fallback 到 qrserver.com 在线服务
            # （国内网络无法访问，会导致二维码空白），直接返回 None 报错，
            # 由 API 层向前端返回明确错误提示。
            print_error(
                "Weread QR: 缺少 qrcode 库，无法本地生成二维码。"
                "请执行 pip install qrcode pillow 后重试"
            )
            return None
        except Exception as e:
            print_error(f"Weread QR: 生成二维码失败: {e}")
            return None

    def _start_polling(self, CallBack):
        """
        启动轮询定时器

        注意：绝不能同步调用 _poll_once！getLoginInfo 是长轮询接口，
        最长可挂起 timeout(70s)。若同步执行，GetCode() 会被阻塞数十秒，
        二维码图片虽然已生成，但前端拿不到 URL，一直停在"正在获取二维码..."。
        因此首次轮询必须延时到后台线程启动，让 GetCode() 立即返回。
        """
        self._poll_start_time = time.time()
        self._poll_timer = Timer(0.5, self._poll_once, args=[CallBack])
        self._poll_timer.daemon = True
        self._poll_timer.start()

    def _poll_once(self, CallBack):
        """单次轮询登录状态"""
        try:
            result = self._check_login_status()
        except Exception as e:
            print_error(f"Weread QR: 轮询异常: {e}")
            result = {"succeed": False, "error": str(e)}

        if result.get("succeed"):
            # getLoginInfo 已确认扫码成功。抓取端点受限不应使登录失败，
            # 更不能把 refreshToken 当成 skey 反复枚举请求。
            from core.weread_client import WereadClient, normalize_account
            from core.config import cfg
            from urllib.parse import quote

            cookies = self._build_cookie_dict()
            cookies.update(wr_vid=result["vid"], wr_skey=result["wr_skey"])
            if result.get("refreshToken"):
                cookies["wr_rt"] = quote(result["refreshToken"], safe="")
            result["cookies"] = cookies
            account = normalize_account({
                "cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
                "vid": result["vid"], "access_token": result["accessToken"],
                "refresh_token": result.get("refreshToken", ""),
                "skey": result["wr_skey"], "device_id": result.get("deviceId"),
            })
            result["deviceId"] = account["device_id"]
            proxy = cfg.get("proxy.http_url", "") if cfg.get("proxy.enabled", False) else ""
            client = WereadClient([account], proxies={"http": proxy, "https": proxy} if proxy else None)
            try:
                client.prepare()
            finally:
                client.close()
            if not self._save_cookies_to_lic(result):
                self._login_error = "扫码成功但凭据保存失败，请重试"
                return
            self._logged_in = True
            self._login_result = result
            print_success("Weread QR: 登录成功，移动端凭据已保存")
            if CallBack:
                try:
                    CallBack(result)
                except Exception as e:
                    print_error(f"Weread QR: CallBack 异常: {type(e).__name__}")
            return

        # 检查超时
        if time.time() - self._poll_start_time > self._poll_timeout:
            print_warning("Weread QR: 扫码登录超时")
            return

        # 继续轮询
        self._poll_timer = Timer(self._poll_interval, self._poll_once, args=[CallBack])
        self._poll_timer.daemon = True
        self._poll_timer.start()

    def _stop_polling(self):
        """停止轮询"""
        if self._poll_timer:
            self._poll_timer.cancel()
            self._poll_timer = None

    def _check_login_status(self) -> Dict[str, Any]:
        """
        检查登录状态
        GET https://weread.qq.com/api/auth/getLoginInfo?uid={uid}&otp=
        
        返回示例:
        - 等待扫码: {"succeed": false, "logicCode": 0, ...}
        - 已扫码待确认: {"succeed": false, "logicCode": 1, ...}
        - 登录成功: {"succeed": true, "vid": "xxx", "accessToken": "xxx", ...}
        - 超时: {"succeed": false, "logicCode": "LOGIN_TIMEOUT", ...}
        - 需要验证码: {"succeed": false, "logicCode": "NEED_OTP", ...}
        """
        try:
            url = f"{WEREAD_BASE}/api/auth/getLoginInfo"
            params = {"uid": self._uid, "otp": ""}
            # 微信读书 getLoginInfo 长轮询：响应可能挂起较久，官方实现用 70s
            resp = self._session.get(url, params=params, timeout=70)

            if resp.status_code != 200:
                return {"succeed": False, "error": f"HTTP {resp.status_code}"}

            data = resp.json()

            # 官方实现先归一化嵌套结构：data = r.get("data", r)
            # 部分环境下登录成功信息在顶层，部分在 data 子层
            inner = data.get("data") or {}

            # 检查是否成功
            succeed = data.get("succeed") or inner.get("succeed")
            logic_code = data.get("logicCode", "")

            if succeed:
                # Only use documented credential fields, including short access tokens.
                layers = [data, inner, inner.get("data") or {}]
                def field(*names):
                    return next((layer[name] for layer in layers if isinstance(layer, dict)
                                 for name in names if layer.get(name)), "")
                cookies = self._build_cookie_dict()
                vid = str(field("webLoginVid", "vid", "userVid", "user_vid") or cookies.get("wr_vid", ""))
                access_token = field("accessToken", "access_token", "token") or cookies.get("wr_skey", "")
                skey = field("skey", "wr_skey") or cookies.get("wr_skey") or access_token
                from urllib.parse import unquote
                refresh_token = field("refreshToken", "refresh_token") or unquote(cookies.get("wr_rt", ""))
                if not vid or not access_token:
                    return {"succeed": False, "logicCode": "INCOMPLETE_LOGIN",
                            "msg": "扫码成功但未获取到登录令牌，请重试"}
                return {
                    "succeed": True, "vid": vid, "accessToken": access_token,
                    "refreshToken": refresh_token, "wr_skey": skey,
                    "deviceId": field("deviceId", "device_id"),
                    "name": field("name", "nickname"),
                }

            # 未登录的各种状态
            if logic_code == "NEED_OTP":
                print_warning("Weread QR: 需要验证码，当前暂不支持 OTP 验证")
                return {"succeed": False, "logicCode": logic_code, "need_otp": True}

            # 注意：微信读书 getLoginInfo 在“等待扫码”阶段就返回
            # logicCode == "LOGIN_TIMEOUT"，这是正常的长轮询状态，必须继续轮询，
            # 不能当作二维码失效（参考 papers3-weread 官方实现）。
            # 只有整体轮询超过 self._poll_timeout 才真正过期。
            if logic_code == "LOGIN_TIMEOUT":
                return {"succeed": False, "logicCode": "WAITING"}

            # 正常等待中
            return {"succeed": False, "logicCode": logic_code}

        except requests.exceptions.Timeout:
            return {"succeed": False, "error": "请求超时"}
        except Exception as e:
            return {"succeed": False, "error": str(e)}

    def _build_cookie_dict(self) -> Dict[str, str]:
        """从 session 构建 Cookie 字典"""
        cookies = {}
        for cookie in self._session.cookies:
            cookies[cookie.name] = cookie.value
        return cookies

    def _save_cookies_to_lic(self, result: Dict[str, Any]):
        """保存 getLoginInfo 确认成功的完整凭据，不用文章接口再次判定登录成败。"""
        try:
            cookies = dict(result.get("cookies") or self._build_cookie_dict())
            vid = str(result.get("vid") or "")
            if not vid or not result.get("accessToken"):
                return False
            cookies.update(wr_vid=vid, wr_skey=result.get("wr_skey") or result["accessToken"])
            cookie_str = "; ".join(f"{name}={value}" for name, value in cookies.items())

            # 保存到 data/wx.lic
            from core.config import Config

            lic_path = "./data/wx.lic"
            os.makedirs(os.path.dirname(lic_path), exist_ok=True)
            if not os.path.exists(lic_path):
                with open(lic_path, "w") as f:
                    f.write("{}")

            cfg = Config(lic_path)
            weread_data = cfg.get("weread_data", {})
            if isinstance(weread_data, str):
                try:
                    weread_data = json.loads(weread_data)
                except Exception:
                    weread_data = {}

            # 多账号：迁移旧格式到 accounts 数组，新账号追加到数组末尾
            accounts = weread_data.get("accounts", None)
            if not isinstance(accounts, list):
                # 旧格式迁移：weread_data.cookie 包成 accounts[0]
                if weread_data.get("cookie"):
                    accounts = [{
                        "cookie": weread_data["cookie"],
                        "vid": weread_data.get("vid", ""),
                        "name": weread_data.get("name", ""),
                    }]
                else:
                    accounts = []

            # 如果相同 vid 已存在，更新它（重新扫码刷新 cookie）
            existing_idx = next(
                (i for i, a in enumerate(accounts) if str(a.get("vid", "")) == str(vid)),
                None,
            )
            account_entry = {
                "cookie": cookie_str,
                "access_token": result.get("accessToken", ""),
                "refresh_token": result.get("refreshToken", ""),
                "skey": result.get("wr_skey") or result.get("accessToken", ""),
                "device_id": result.get("deviceId", ""),
                "vid": str(vid),
                "name": result.get("name", "") or "",
            }
            if existing_idx is not None:
                # 保留原有 name（如果用户改过）
                account_entry["name"] = accounts[existing_idx].get("name", "")
                accounts[existing_idx] = account_entry
            else:
                accounts.append(account_entry)

            # 写回 accounts 数组，清掉旧格式字段
            weread_data["accounts"] = accounts
            for k in ("cookie", "vid", "name", "ticket"):
                weread_data.pop(k, None)

            cfg.set("weread_data", weread_data)
            print_success(f"Weread QR: 凭据已保存，当前共 {len(accounts)} 个账号")
            return True

        except Exception as e:
            print_error(f"Weread QR: 保存凭据失败: {type(e).__name__}")
            return False


# ---- 模块级实例 ----
WEREAD_QR_LOGIN = WereadQRLogin()
