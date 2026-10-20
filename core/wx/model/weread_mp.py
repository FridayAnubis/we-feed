"""Collect WeChat Official Account history through the mobile WeRead workflow."""

import re
import time
from urllib.parse import quote

from bs4 import BeautifulSoup

from core.weread_client import WereadAPIError as WereadMPAPIError
from core.log import logger
from core.models.feed import Feed
from core.print import print_info, print_warning
from core.wx.model.weread import MpsWeread, WEREAD_APP_BASE


WEREAD_WEB_BASE = "https://weread.qq.com"
MP_PAGE_SIZE = 20

def mp_book_id(value):
    """Accept existing WeRead IDs, numeric uin and backend Base64 fakeid."""
    import base64
    from urllib.parse import unquote
    value = unquote(str(value or "").strip())
    if re.fullmatch(r"MP_WXS_\d+", value):
        return value
    if value.isdigit():
        return f"MP_WXS_{value}"
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), validate=True).decode()
        return f"MP_WXS_{decoded}" if decoded.isdigit() else ""
    except (ValueError, UnicodeDecodeError):
        return ""

def clean_article_title(raw_title: str, max_length: int = 120) -> str:
    """清洗文章标题，防止短图文将整段正文当作标题"""
    if not raw_title:
        return ""
    lines = [line.strip() for line in raw_title.splitlines() if line.strip()]
    if not lines:
        return ""
    first_line = lines[0]
    if len(first_line) > max_length:
        sentences = re.split(r'([。！？!?])', first_line)
        if len(sentences) >= 2 and len(sentences[0] + sentences[1]) <= max_length:
            first_line = sentences[0] + sentences[1]
        else:
            first_line = first_line[:max_length].rstrip() + "..."
    return first_line.strip() or ""



def build_mp_url(original_id: str) -> str:
    original_id = str(original_id or "").strip()
    if not original_id:
        return ""
    # 微信文章短链 token 的字符集是 URL-safe 的（A-Za-z0-9_-），
    # 微信读书返回的 ID 里把 '_' 写成了 '~'，必须换回来，否则微信返回"参数错误"。（如 ySsAk3lUFT0Wh~9IZCtr-g）
    # （实测 2026-09-23：149 篇含 '~' 的链接全部参数错误，替换后 100% 可打开。）
    original_id = original_id.replace("~", "_")
    article_token = quote(original_id, safe="")
    return f"https://mp.weixin.qq.com/s/{article_token}"


def build_mp_link_from_review_id(review_id: str, book_id: str = "") -> str:
    """从 WeRead 的 reviewId 推导公众号原文链接。

    reviewId 形如 ``MP_WXS_<bookId>_<articleToken>``，末尾段即为
    mp.weixin.qq.com 原文短链的 token。微信读书把 token 里的 ``_`` 写成了
    ``~``，须还原为 ``_``，否则微信返回"参数错误"。新版接口 ``/api/mp/cover``
    不返回 originalId，只能由此推导。
    """
    review_id = str(review_id or "").strip()
    if not review_id:
        return ""
    token = review_id
    prefix = f"{book_id}_" if book_id else ""
    if prefix and review_id.startswith(prefix):
        token = review_id[len(prefix):]
    elif "_" in token:
        token = token.split("_")[-1]
    # 前缀拆分依赖原始的 '_'，'~' → '_' 的还原必须放在拆分之后
    token = token.replace("~", "_")
    return f"https://mp.weixin.qq.com/s/{quote(token, safe='')}"


def _raise_response_error(payload: dict):
    code = payload.get("errCode", payload.get("errcode", 0))
    try:
        code = int(code or 0)
    except (TypeError, ValueError):
        code = 0
    if code == 0:
        return
    message = payload.get("errMsg") or payload.get("errmsg") or payload.get("errlog") or str(code)
    raise WereadMPAPIError(
        code,
        message,
        retriable=code not in (-2041, -2012, -2010),
    )


def parse_mp_articles(payload: dict):
    if not isinstance(payload, dict):
        raise WereadMPAPIError("invalid_response", "response is not an object")
    _raise_response_error(payload)

    # mp/chapters 返回平铺列表；兼容旧 Web 的 reviews[].subReviews 分组。
    candidates = [payload[key] for key in ("data", "chapters", "list", "reviews")
                  if isinstance(payload.get(key), list)]
    groups = next((value for value in candidates if value), candidates[0] if candidates else None)
    if not isinstance(groups, list):
        raise WereadMPAPIError("invalid_response", "article list is missing or invalid")
    articles = []
    for group in groups:
        if not isinstance(group, dict):
            raise WereadMPAPIError("invalid_response", "article entry is not an object")
        group_time = group.get("createTime", 0)
        reviews = group.get("subReviews") if "subReviews" in group else [group]
        if not isinstance(reviews, list):
            raise WereadMPAPIError("invalid_response", "subReviews is not a list")
        for sub_review in reviews or []:
            if not isinstance(sub_review, dict):
                raise WereadMPAPIError("invalid_response", "review is not an object")
            review = sub_review.get("review") or sub_review
            if not isinstance(review, dict):
                raise WereadMPAPIError("invalid_response", "review is not an object")
            mp_info = review.get("mpInfo") or {}
            if not isinstance(mp_info, dict):
                raise WereadMPAPIError("invalid_response", "mpInfo is not an object")
            review_id = review.get("reviewId") or sub_review.get("reviewId")
            if not review_id:
                raise WereadMPAPIError("invalid_response", "article has no reviewId")
            create_time = review.get("createTime") or group_time or 0
            update_time = mp_info.get("time") or create_time
            try:
                update_time = int(update_time)
                update_time = update_time // 1000 if update_time > 1_000_000_000_000 else max(update_time, 0)
            except (TypeError, ValueError):
                update_time = 0
            articles.append({
                "aid": review_id,
                "id": review_id,
                "title": clean_article_title(mp_info.get("title") or review.get("title", "")),
                "link": mp_info.get("doc_url") or build_mp_url(mp_info.get("originalId", "")) or build_mp_link_from_review_id(review_id),
                "cover": mp_info.get("pic_url") or mp_info.get("thumbUrl") or review.get("cover", ""),
                "digest": mp_info.get("content") or mp_info.get("digest") or review.get("content", ""),
                "content": "",
                "create_time": create_time,
                "update_time": update_time,
                "read_num": mp_info.get("readNum", 0),
                "like_num": mp_info.get("likeNum", 0),
                "item_show_type": 0,
            })
    cover = payload.get("_cover")
    if cover and not any(item["aid"] == cover["reviewId"] for item in articles):
        extra, _ = parse_mp_articles({"reviews": [cover]})
        articles = extra + articles
    return articles, len(groups)


def extract_mp_content(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    content = soup.select_one("#js_content") or soup.select_one(".rich_media_content")
    if content is None:
        raise WereadMPAPIError("invalid_content", "article body was not found")
    for element in content.select("script, style"):
        element.decompose()
    for img in content.select("img[data-src]"):
        img["src"] = img["data-src"]
        del img["data-src"]
    if not content.get_text(strip=True) and not content.select("img, video, audio, iframe"):
        raise WereadMPAPIError("invalid_content", "article body is empty")
    body = content.decode_contents().strip()
    from core.article_content import is_usable_article_content
    if not is_usable_article_content(body):
        raise WereadMPAPIError("invalid_content", "article body is an error page")
    return body


class MpsWereadMP(MpsWeread):
    """WeRead-backed collector for existing ``MP_WXS_*`` feeds."""

    def _get_feed_update_time(self, mp_id: str) -> int:
        from core.db import DB

        session = DB.get_session()
        try:
            row = session.query(Feed.update_time).filter(Feed.id == mp_id).first()
            return int(row[0] or 0) if row else 0
        finally:
            session.close()

    def _get_catchup_page_limit(self, requested_pages: int) -> int:
        from core.config import cfg

        configured_limit = int(cfg.get("weread.mp_max_pages", 20) or 20)
        return max(requested_pages, configured_limit, 1)

    def _get_content_interval(self) -> float:
        from core.config import cfg

        return max(float(cfg.get("weread.content_interval", 2) or 0), 0)

    def _get_page_interval(self) -> float:
        from core.config import cfg

        return max(float(cfg.get("weread.page_interval", 1) or 0), 0)

    def _request_headers(self, include_ticket=False):
        """已并入基类 ``_weread_headers``，保留此方法仅作兼容别名。"""
        return self._weread_headers(include_ticket=include_ticket)

    def _app_json(self, path, params=None, prepare=False):
        try:
            return self._get_weread_client().json(path, params, prepare=prepare)
        finally:
            self._sync_weread_client()

    def _get_mp_articles_page(self, book_id: str, offset=0):
        """Cover + chapters first; restart the backup's own cursor on failure."""
        if not hasattr(self, "_history_source"):
            self._history_source = "chapters"
            self._history_synckey = 0
            self._history_cursors = set()
            self.history_warning = ""
        cover = None
        if offset == 0:
            try:
                cover = self._get_mp_cover(book_id)
            except WereadMPAPIError as exc:
                logger.warning(f"最新文章预取失败: {exc.code}")
        if self._history_source == "chapters":
            try:
                payload = self._app_json("/mp/chapters", {
                    "bookId": book_id, "offset": offset, "count": MP_PAGE_SIZE, "pf": "android",
                }, prepare=True)
                _, count = parse_mp_articles(payload)
                payload.setdefault("hasMore", count >= MP_PAGE_SIZE)
            except WereadMPAPIError as exc:
                self.history_warning = f"历史章节接口失败({exc.code})，改用 article/list"
                logger.warning(self.history_warning)
                self._history_source = "article/list"
        if self._history_source == "article/list":
            payload = self._app_json("/article/list", {
                "bookId": book_id, "synckey": self._history_synckey,
            }, prepare=True)
            parse_mp_articles(payload)
            # synckey is NOT an offset. Follow it only when the API says more exist.
            more = payload.get("hasMore") in (True, 1, "1")
            cursor = payload.get("synckey")
            if more and (cursor is None or str(cursor) == str(self._history_synckey)
                         or str(cursor) in self._history_cursors):
                raise WereadMPAPIError("stalled_cursor", "article/list 游标未推进，补抓未完成")
            if more:
                self._history_cursors.add(str(cursor))
                self._history_synckey = cursor
            payload["hasMore"] = more
        payload["_source"] = self._history_source
        if cover and cover.get("reviewId"):
            payload["_cover"] = {
                "reviewId": cover["reviewId"], "title": cover.get("title", ""),
                "cover": cover.get("pic", ""), "createTime": cover.get("createTime", 0),
                "mpInfo": cover.get("mpInfo") or {},
            }
        self.history_source = self._history_source
        return payload

    def _get_mp_cover(self, book_id: str) -> dict:
        """Mobile cover first, then the existing Web cover."""
        cached = getattr(self, "_cover_cache", {}).get(book_id)
        if cached:
            return cached
        try:
            payload = self._app_json("/mp/cover", {"bookId": book_id}, prepare=True)
            if not payload.get("reviewId"):
                raise WereadMPAPIError("empty_cover", "公众号未返回最新文章")
        except WereadMPAPIError:
            response = self._weread_http_get(
                f"{WEREAD_WEB_BASE}/api/mp/cover", params={"bookId": book_id},
            )
            if response.status_code != 200:
                raise WereadMPAPIError(response.status_code, "cover 请求失败")
            try:
                payload = response.json()
            except ValueError as exc:
                raise WereadMPAPIError("invalid_json", "cover is not JSON") from exc
            if not isinstance(payload, dict):
                raise WereadMPAPIError("invalid_response", "cover is not an object")
            _raise_response_error(payload)
            if not payload.get("reviewId"):
                raise WereadMPAPIError("empty_cover", "公众号未返回最新文章")
        if not hasattr(self, "_cover_cache"):
            self._cover_cache = {}
        self._cover_cache[book_id] = payload
        return payload

    def _resolve_article(self, item, book_id):
        """Resolve missing metadata via review/single before fetching an article page."""
        if not item.get("update_time") or not item.get("link"):
            try:
                data = self._app_json("/review/single", {"reviewId": item["aid"]})
                review = data.get("review") or data
                parsed, _ = parse_mp_articles({"reviews": [review]})
                if parsed:
                    for key in ("link", "update_time", "create_time", "title", "cover", "digest"):
                        if parsed[0].get(key):
                            item[key] = parsed[0][key]
            except WereadMPAPIError as exc:
                logger.warning(f"文章元数据解析失败: {exc.code}")
        if not item.get("link"):
            item["link"] = build_mp_link_from_review_id(item["aid"], book_id)
        if not item.get("update_time"):
            article = self._get_original_article(item["link"], browser_fallback=True)
            if not article.get("publish_time"):
                raise WereadMPAPIError("invalid_publish_time", "原文发布时间提取失败")
            item["update_time"] = article["publish_time"]
        return item

    def _get_original_article(self, link, browser_fallback=False):
        """Public article HTTP first, existing browser fetcher last; no account headers."""
        import requests
        from urllib.parse import urlsplit
        if urlsplit(link).hostname != "mp.weixin.qq.com":
            raise WereadMPAPIError("invalid_link", "公众号原文链接无效")
        cache = getattr(self, "_original_cache", {})
        if link in cache and (not browser_fallback or cache[link].get("publish_time")):
            return cache[link]
        article = {}
        try:
            response = requests.get(link, headers={
                "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Mobile MicroMessenger/8.0.49",
                "Referer": "https://mp.weixin.qq.com/", "X-Requested-With": "com.tencent.mm",
            }, proxies=self._get_proxies(), timeout=(10, 25))
            if response.status_code == 200:
                response.encoding = "utf-8"
                content = extract_mp_content(response.text)
                match = re.search(r"\b(?:ct|create_time|publish_time)\s*=\s*[\"']?(\d{10})\b", response.text)
                article = {"content": content, "publish_time": int(match.group(1)) if match else 0}
        except (requests.RequestException, WereadMPAPIError):
            pass
        if browser_fallback and (not article.get("content") or not article.get("publish_time")):
            from driver.wxarticle import Web
            rendered = Web.get_article_content(link)
            if rendered.get("content") or rendered.get("publish_time"):
                article.update({k: v for k, v in rendered.items() if v})
        from core.article_content import is_usable_article_content
        if article.get("content") and not is_usable_article_content(article["content"]):
            article.pop("content")
        if article.get("content"):
            if len(cache) >= 20:
                cache.pop(next(iter(cache)))
            cache[link] = article
            self._original_cache = cache
        return article

    def _is_article_gathered(self, mp_id: str, review_id: str) -> bool:
        """判断 reviewId 是否已入库（跨会话增量，避免重复推送）。

        Article.id 存储格式为 ``{mp_id}-{aid}`` 并去掉 ``MP_WXS_`` 前缀，
        与 ``Db.add_article`` 的归一化规则保持一致。
        """
        if not mp_id or not review_id:
            return False
        from core.db import DB
        from core.models.article import Article

        session = DB.get_session()
        try:
            stored_id = f"{mp_id}-{review_id}".replace("MP_WXS_", "")
            row = session.query(Article.id).filter(
                Article.mp_id == mp_id,
                Article.id == stored_id,
            ).first()
            return row is not None
        except Exception:
            return False
        finally:
            session.close()

    def _get_mp_content(self, review_id: str):
        link = getattr(self, "_article_links", {}).get(review_id) or build_mp_link_from_review_id(review_id)
        original = self._get_original_article(link)
        if original.get("content"):
            return original["content"]
        try:
            response = self._weread_http_get(
                f"{WEREAD_WEB_BASE}/web/mp/content",
                params={"reviewId": review_id},
            )
            if response.status_code == 200:
                return extract_mp_content(response.text)
        except WereadMPAPIError:
            pass
        rendered = self._get_original_article(link, browser_fallback=True)
        from core.article_content import is_usable_article_content
        if is_usable_article_content(rendered.get("content")):
            return rendered["content"]
        raise WereadMPAPIError("invalid_content", "原文、微信读书和浏览器均未获取到正文")

    def _save_item(self, callback, item, title, mp_id):
        def checked(data):
            if not callback(data):
                raise WereadMPAPIError("save_failed", "文章入库失败，未推进同步状态")
            return True
        super().FillBack(CallBack=checked, data=item, Ext_Data={"mp_title": title, "mp_id": mp_id})

    def _collect_latest_via_cover(
        self,
        book_id: str,
        Mps_id: str,
        Mps_title: str,
        gather_content: bool,
        CallBack=None,
        Item_Over_CallBack=None,
    ) -> int:
        """兜底方案：通过 /api/mp/cover 只采集最新一篇文章。"""
        cover = self._get_mp_cover(book_id)
        review_id = (cover.get("reviewId") or "").strip()
        title = clean_article_title((cover.get("title") or "").strip())
        self.response_valid = True

        if not review_id:
            raise WereadMPAPIError(
                "empty_cover",
                f"公众号「{Mps_title}」暂无文章",
                retriable=False,
            )

        # 跨会话增量：最新文章已入库则无需重复采集
        if self._is_article_gathered(Mps_id, review_id):
            print_info(f"无新文章：最新《{title}》已在库中，跳过")
            return self._get_feed_update_time(Mps_id)

        print_info(f"发现新文章: {title}")
        link = build_mp_link_from_review_id(review_id, book_id)
        article = self._get_original_article(link, browser_fallback=True)
        publish_time = article.get("publish_time") or 0
        if not publish_time:
            raise WereadMPAPIError(
                "invalid_publish_time",
                article.get("fetch_error") or "原文发布时间提取失败",
            )
        item = {
            "aid": review_id,
            "id": review_id,
            "title": title,
            "link": link,
            "cover": cover.get("pic") or "",
            "digest": cover.get("digest") or "",
            "content": "",
            "create_time": int(time.time()),
            "update_time": publish_time,
            "read_num": 0,
            "like_num": 0,
            "item_show_type": 0,
            "copyright_stat": 1,
            "mp_id": Mps_id,
        }
        if gather_content:
            content_interval = self._get_content_interval()
            if content_interval:
                time.sleep(content_interval)
            try:
                item["content"] = self._get_mp_content(review_id)
            except WereadMPAPIError as exc:
                logger.warning(f"微信读书正文获取失败 [{review_id}]: {exc}")
        if CallBack is not None:
            self._save_item(CallBack, item, Mps_title, Mps_id)
        if Item_Over_CallBack is not None:
            Item_Over_CallBack(item)
        return int(item["update_time"])

    def _collect_via_article_list(
        self,
        book_id: str,
        Mps_id: str,
        Mps_title: str,
        gather_content: bool,
        start_page: int,
        MaxPage: int,
        CallBack=None,
        Item_Over_CallBack=None,
    ) -> int:
        """增量补抓：翻页扫描文章列表，采集所有尚未入库的文章。

        停止条件（满足其一即停止翻页）：
        - 普通增量任务遇到整页已入库文章（置顶 cover 不作为停止边界）
        - 列表翻完（空页）或达到 ``weread.mp_max_pages`` 页数上限

        注意：不以 ``Feed.update_time`` 作为停止边界。旧版 cover 兜底模式会把
        **抓取时间**（而非文章发布时间）写入 update_time，若按时间停止，
        cover 时代漏采的文章（发布早于上次抓取、但不是当时最新一篇）将被永久
        跳过。以入库记录为边界可以在 cover → 列表切换后一次性补齐这些漏采文章，
        稳态下第一页全部已入库时即可停止；指定 start_page 的历史任务继续翻页。

        翻页间隔 ``weread.page_interval``、正文请求间隔 ``weread.content_interval``
        用于控制请求频率，避免触发微信读书流控。
        """
        start_page = max(int(start_page or 0), 0)
        end_page = max(int(MaxPage or 1), start_page + 1)
        offset = start_page * MP_PAGE_SIZE
        latest_publish_time = 0
        content_failures = []
        metadata_failures = []
        previous_update_time = self._get_feed_update_time(Mps_id)
        requested_pages = end_page - start_page
        page_limit = self._get_catchup_page_limit(requested_pages)
        reached_gathered = not previous_update_time
        content_request_count = 0
        content_interval = self._get_content_interval()
        page_interval = self._get_page_interval()
        new_count = 0

        for page in range(start_page, start_page + page_limit):
            if page > start_page and page_interval:
                time.sleep(page_interval)
            payload = self._get_mp_articles_page(book_id, offset=offset)
            articles, group_count = parse_mp_articles(payload)
            self.response_valid = True
            page_new_count = 0
            page_gathered_count = 0

            for item in articles:
                if super().HasGathered(item["aid"]):
                    continue
                if self._is_article_gathered(Mps_id, item["aid"]):
                    reached_gathered = True
                    # A known pinned cover must not stop historical backfill.
                    if item["aid"] != (payload.get("_cover") or {}).get("reviewId"):
                        page_gathered_count += 1
                    continue
                try:
                    item = self._resolve_article(item, book_id)
                except WereadMPAPIError as exc:
                    logger.warning(f"文章元数据缺失 [{item['aid']}]: {exc.code}")
                    metadata_failures.append(item["aid"])
                    page_new_count += 1
                    continue
                publish_time = int(item["update_time"])
                if not hasattr(self, "_article_links"):
                    self._article_links = {}
                self._article_links[item["aid"]] = item["link"]
                page_new_count += 1
                item["mp_id"] = Mps_id
                latest_publish_time = max(latest_publish_time, publish_time)
                if gather_content:
                    if content_request_count and content_interval:
                        time.sleep(content_interval)
                    content_request_count += 1
                    try:
                        item["content"] = self._get_mp_content(item["aid"])
                    except WereadMPAPIError as exc:
                        logger.warning(f"微信读书正文获取失败 [{item['aid']}]: {exc}")
                        content_failures.append(item["aid"])
                        continue
                if CallBack is not None:
                    self._save_item(CallBack, item, Mps_title, Mps_id)
                if Item_Over_CallBack is not None:
                    Item_Over_CallBack(item)
                new_count += 1

            if group_count == 0 or payload.get("hasMore") in (False, 0, "0"):
                reached_gathered = True
                break
            # 按接口原始条目数翻页；去重后的文章数不能作为 offset 增量。
            offset += group_count
            if previous_update_time and not start_page and page_gathered_count and not page_new_count:
                break

        if metadata_failures:
            raise WereadMPAPIError(
                "invalid_publish_time", f"{len(metadata_failures)} 篇文章元数据仍不完整，其余文章已处理",
            )
        if content_failures:
            raise WereadMPAPIError(
                "content_incomplete",
                f"{len(content_failures)} article bodies could not be fetched",
            )
        if not reached_gathered:
            raise WereadMPAPIError(
                "backlog_incomplete",
                f"catch-up did not reach any gathered article after {page_limit} pages",
            )

        print_info(f"[{Mps_title}] 增量补抓完成: 新增 {new_count} 篇")
        return latest_publish_time or previous_update_time

    def get_Articles(
        self,
        faker_id: str = None,
        Mps_id: str = None,
        Mps_title="",
        CallBack=None,
        start_page: int = 0,
        MaxPage: int = 1,
        interval=10,
        Gather_Content=False,
        Item_Over_CallBack=None,
        Over_CallBack=None,
    ):
        """Collect one existing WeChat feed without changing its RSS identity.

        主路径走 ``i.weread.qq.com/mp/chapters`` 做增量补抓：从最新一页开始翻页，
        采集尚未入库的文章，整页已入库时停止普通增量任务。chapters 不可用时
        使用 article/list；历史列表均不可用时才尝试 cover，并明确报告仅有单篇兜底。
        """
        self.articles = []
        self.aids = []
        self.get_token()
        self.start_time = time.time()
        self.response_valid = False
        self.last_error = None
        self._history_source = "chapters"
        self._history_synckey = 0
        self._history_cursors = set()
        self._cover_cache = {}
        self._original_cache = {}
        self._article_links = {}
        self.history_warning = ""
        self.history_source = "chapters"
        self._load_weread_auth()

        if not self._weread_cookies:
            raise WereadMPAPIError(
                "missing_cookie",
                "WEREAD_COOKIE is required",
                retriable=False,
            )

        book_id = mp_book_id(Mps_id) or mp_book_id(faker_id)
        if not str(book_id or "").startswith("MP_WXS_"):
            raise WereadMPAPIError(
                "invalid_book_id",
                "the feed id must use the MP_WXS_ prefix",
                retriable=False,
            )

        # History requests do not require mutating the user's shelf first.
        gather_content = bool(Gather_Content or self.Gather_Content)

        print_info(f"微信读书公众号采集模式: {Mps_title} ({book_id})")
        try:
            try:
                latest_publish_time = self._collect_via_article_list(
                    book_id,
                    Mps_id,
                    Mps_title,
                    gather_content,
                    start_page,
                    MaxPage,
                    CallBack=CallBack,
                    Item_Over_CallBack=Item_Over_CallBack,
                )
            except WereadMPAPIError as exc:
                if self.response_valid or start_page:
                    # 列表接口本身可用（已成功翻页），属于正文缺失或补抓不完整
                    # 等真实错误，直接上抛，下次任务重试。
                    raise
                print_warning(
                    f"[{Mps_title}] 文章列表接口不可用({exc})，回退到仅取最新一篇"
                )
                latest_publish_time = self._collect_latest_via_cover(
                    book_id,
                    Mps_id,
                    Mps_title,
                    gather_content,
                    CallBack=CallBack,
                    Item_Over_CallBack=Item_Over_CallBack,
                )
                self.history_source = "cover"
                self.history_warning = f"历史列表不可用({exc.code})，本次仅获取最新一篇"

            self.update_mps(
                Mps_id,
                Feed(
                    sync_time=int(time.time()),
                    update_time=latest_publish_time or None,
                ),
            )
        except Exception as exc:
            self.last_error = {
                "message": str(exc),
                "code": getattr(exc, "code", "weread_mp_error"),
                "retriable": getattr(exc, "retriable", True),
            }
            super().Over(CallBack=Over_CallBack)
            raise
        finally:
            client = getattr(self, "_weread_client", None)
            if client:
                client.close()

        super().Over(CallBack=Over_CallBack)
