"""Mobile WeRead workflow adapted from wechat-mp-tools (see docs/history-fetch.md)."""

import hashlib
import json
import random
import threading
import time
from urllib.parse import quote, unquote, urlsplit

import requests

APP_BASE = "https://i.weread.qq.com"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_cooldowns = {}
_state_lock = threading.Lock()


class WereadAPIError(Exception):
    def __init__(self, code, message, retriable=True):
        super().__init__(f"WeRead MP API error {code}: {message}")
        self.code = code
        self.message = message
        self.retriable = retriable


def cookie_dict(value):
    return dict(part.strip().split("=", 1) for part in (value or "").split(";") if "=" in part)


def normalize_account(account):
    """Never infer token validity from its length or substitute a refresh token."""
    result = dict(account)
    cookies = cookie_dict(result.get("cookie", ""))
    result["vid"] = str(result.get("vid") or cookies.get("wr_vid") or "")
    result["access_token"] = (
        result.get("access_token") or result.get("accessToken")
        or cookies.get("accessToken") or cookies.get("wr_skey") or ""
    )
    result["skey"] = result.get("skey") or cookies.get("wr_skey") or result["access_token"]
    result["refresh_token"] = (
        result.get("refresh_token") or result.get("refreshToken")
        or unquote(cookies.get("wr_rt", ""))
    )
    # Preserve the binding; older sessions use the reference client's default.
    result["device_id"] = str(result.get("device_id") or result.get("deviceId") or "410515152")
    if not result.get("cookie") and result["vid"] and result["skey"]:
        result["cookie"] = f"wr_vid={result['vid']}; wr_skey={result['skey']}"
    return result


def response_error(response):
    code = 0
    message = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            code = int(payload.get("errcode", payload.get("errCode", 0)) or 0)
            message = payload.get("errmsg") or payload.get("errMsg") or payload.get("errlog") or ""
    except (ValueError, TypeError):
        pass
    if not code and response.status_code != 200:
        code = response.status_code
    return code, message or (f"HTTP {response.status_code}" if code else "")


def persist_account(previous, updated, lic_path="data/wx.lic"):
    """Merge a refreshed account without replacing other accounts or wx credentials."""
    from core.config import Config
    from pathlib import Path
    import portalocker

    path = Path(lic_path)
    if not path.exists():
        return
    with portalocker.Lock(str(path) + ".weread.lock", timeout=10):
        cfg = Config(str(path))
        data = cfg.get("weread_data", {}) or {}
        if isinstance(data, str):
            data = json.loads(data)
        accounts = data.get("accounts")
        legacy = not isinstance(accounts, list)
        accounts = accounts if not legacy else [data]
        for account in accounts:
            saved = normalize_account(account)
            if saved["vid"] != previous["vid"]:
                continue
            # A new QR login or manual replacement wins over this in-flight refresh.
            if (saved["access_token"] != previous["access_token"]
                    or saved.get("cookie") != previous.get("cookie")):
                return
            for key in ("cookie", "access_token", "refresh_token", "skey", "device_id"):
                account[key] = updated[key]
            if not legacy:
                data["accounts"] = accounts
            cfg.set("weread_data", data)
            return


class WereadClient:
    """Bounded refresh/activation/failover with endpoint-specific cooldowns."""

    def __init__(self, accounts, proxies=None, on_refresh=None, session=None):
        self.accounts = [normalize_account(a) for a in accounts]
        self.index = 0
        self.proxies = proxies
        self.on_refresh = on_refresh
        self.session = session or requests.Session()
        self._prepared = set()
        self.last_error = None

    @property
    def account(self):
        return self.accounts[self.index]

    def headers(self):
        account = self.account
        return {
            "vid": account["vid"],
            "accessToken": account["access_token"],
            "skey": account["skey"],
            "Cookie": account.get("cookie", ""),
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }

    def _key(self):
        account = self.account
        return hashlib.sha256(
            f"{account['vid']}:{account['access_token']}".encode()
        ).hexdigest()

    def _request(self, method, url, **kwargs):
        # Headers always supplied explicitly; never reuse cookies from another account.
        self.session.cookies.clear()
        try:
            return getattr(self.session, method)(
                url, proxies=self.proxies, timeout=kwargs.pop("timeout", (10, 20)),
                allow_redirects=False, **kwargs,
            )
        except requests.RequestException as exc:
            raise WereadAPIError("network_error", f"{urlsplit(url).path}: {type(exc).__name__}") from exc

    def activate(self):
        fields = (
            "shelf bookInventory shelfLecture notifications config gift payItem "
            "reviewTimeline follower readingExchange chat chatRemovedSynckey "
            "wechatFriend inBackground refluxSynckey following applyList mcard "
            "discoverFeedSynckey rateSynckey browse medal medalUrl regionResource "
            "discoverColumnSynckey friendReviewSynckey wehearSyncKey"
        )
        payload = dict.fromkeys(fields.split(), 0)
        payload.update(balance=0.0, preferTab=1, localBrowseTab="", localCommunityTab="")
        try:
            response = self._request("post", f"{APP_BASE}/mobileSync",
                                     json=payload, headers=self.headers())
            code, _ = response_error(response)
            data = response.json()
            if code or not isinstance(data, dict):
                return False
            # Same warm-up sequence as the reference project; no recursive recovery.
            for path, params in (
                ("/shelf/sync", {"synckey": 0}),
                ("/mp/list", {"listType": 0, "count": 50, "synckey": 0}),
            ):
                try:
                    self._request("get", APP_BASE + path, params=params, headers=self.headers())
                except WereadAPIError:
                    pass
            return True
        except (WereadAPIError, ValueError):
            return False

    def refresh(self):
        from core.weread_signer import gen_signature

        previous = dict(self.account)
        token = previous["refresh_token"]
        if not token:
            return False
        stamp, nonce = int(time.time() * 1000), random.randint(0, 999)
        payload = {
            "refreshToken": token, "deviceId": previous["device_id"],
            "wxToken": 0, "inBackground": 0, "trackId": "", "kickType": 1,
            "refCgi": "", "timestamp": stamp, "random": nonce,
            "signature": gen_signature(["", previous["device_id"], str(stamp), str(nonce), token]),
            "virtualChannelId": "", "deviceName": "Android",
        }
        try:
            response = self._request("post", f"{APP_BASE}/login", json=payload, headers={
                "User-Agent": "WeRead/7.5.0 (Android; 33; arm64-v8a)",
                "Content-Type": "application/json; charset=UTF-8",
            })
            code, _ = response_error(response)
            data = response.json()
            if code or not isinstance(data, dict) or not data.get("accessToken"):
                return False
            if data.get("vid") and str(data["vid"]) != previous["vid"]:
                return False
            updated = dict(previous)
            updated.update(
                access_token=data["accessToken"],
                skey=data.get("skey") or data["accessToken"],
                refresh_token=data.get("refreshToken") or token,
            )
            cookies = cookie_dict(previous.get("cookie"))
            cookies.update(wr_vid=updated["vid"], wr_skey=updated["skey"],
                           wr_rt=quote(updated["refresh_token"], safe=""))
            cookies.pop("accessToken", None)
            updated["cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
            self.accounts[self.index] = updated
            if self.on_refresh:
                try:
                    self.on_refresh(previous, updated)
                except Exception:
                    import logging
                    logging.getLogger(__name__).warning("微信读书令牌已续期，但持久化失败")
            self.activate()
            self._prepared.add(self._key())
            return True
        except (WereadAPIError, ValueError):
            return False

    def prepare(self):
        key = self._key()
        if key not in self._prepared:
            self._prepared.add(key)
            self.activate()

    def get(self, path, params=None, prepare=False, timeout=(10, 20)):
        if not self.accounts:
            raise WereadAPIError("missing_cookie", "请先扫码授权微信读书", False)
        last_error = None
        start = self.index
        for attempt in range(len(self.accounts)):
            self.index = (start + attempt) % len(self.accounts)
            key = (self._key(), path)
            with _state_lock:
                cooldown = _cooldowns.get(key)
            if cooldown and cooldown[0] > time.monotonic():
                last_error = cooldown[1]
                continue
            if prepare:
                self.prepare()
            refreshed = activated = False
            while True:
                try:
                    response = self._request("get", APP_BASE + path, params=params,
                                             headers=self.headers(), timeout=timeout)
                except WereadAPIError as exc:
                    last_error = exc
                    break
                code, message = response_error(response)
                if not code:
                    self.last_error = None
                    return response
                if (response.status_code == 401 or code in (-2010, -2012)) and not refreshed:
                    refreshed = True
                    if self.refresh():
                        continue
                if code == -2041 and not activated:
                    activated = True
                    if self.activate():
                        continue
                last_error = WereadAPIError(code, f"{path}: {message}", code not in (401, -2010, -2012))
                # Cool down only this endpoint: another endpoint may still work.
                if response.status_code in (403, 429, 499) or code in (-2041, -2013, -2014):
                    with _state_lock:
                        now = time.monotonic()
                        for old_key, value in list(_cooldowns.items()):
                            if value[0] <= now:
                                del _cooldowns[old_key]
                        _cooldowns[(self._key(), path)] = (now + 60, last_error)
                break
        self.last_error = last_error or WereadAPIError("unavailable", "暂无可用微信读书账号")
        raise self.last_error

    def json(self, path, params=None, prepare=False):
        response = self.get(path, params=params, prepare=prepare)
        try:
            payload = response.json()
        except ValueError as exc:
            raise WereadAPIError("invalid_json", f"{path}: response is not JSON") from exc
        if not isinstance(payload, dict):
            raise WereadAPIError("invalid_response", f"{path}: response is not an object")
        return payload

    def close(self):
        self.session.close()
