import unittest
from unittest.mock import Mock, patch, call

from core.wx.model.weread_mp import (
    MpsWereadMP,
    WereadMPAPIError,
    build_mp_link_from_review_id,
    build_mp_url,
    extract_mp_content,
    parse_mp_articles,
)


class WereadMpParsingTest(unittest.TestCase):
    def test_parse_mp_articles_maps_article_fields(self):
        payload = {
            "synckey": 1782304237,
            "reviews": [
                {
                    "createTime": 1778580000,
                    "subReviews": [
                        {
                            "reviewId": "MP_WXS_1_review-1",
                            "review": {
                                "reviewId": "MP_WXS_1_review-1",
                                "createTime": 1778580001,
                                "mpInfo": {
                                    "title": "Article title",
                                    "content": "Article summary",
                                    "time": 1778580002,
                                    "originalId": "abc~def",
                                    "pic_url": "https://example.test/cover.jpg",
                                    "readNum": 12,
                                    "likeNum": 3,
                                },
                            },
                        }
                    ],
                }
            ],
        }

        articles, group_count = parse_mp_articles(payload)

        self.assertEqual(group_count, 1)
        self.assertEqual(len(articles), 1)
        self.assertEqual(
            articles[0],
            {
                "aid": "MP_WXS_1_review-1",
                "id": "MP_WXS_1_review-1",
                "title": "Article title",
                "link": "https://mp.weixin.qq.com/s/abc_def",
                "cover": "https://example.test/cover.jpg",
                "digest": "Article summary",
                "content": "",
                "create_time": 1778580001,
                "update_time": 1778580002,
                "read_num": 12,
                "like_num": 3,
                "item_show_type": 0,
            },
        )

    def test_parse_app_articles_preserves_id_url_and_publish_time(self):
        articles, count = parse_mp_articles({"reviews": [{"review": {
            "reviewId": "MP_WXS_1_article", "createTime": 1792303800,
            "mpInfo": {"title": "History", "originalId": "abc~def", "time": 1792303881},
        }}]})

        self.assertEqual(count, 1)
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["id"], "MP_WXS_1_article")
        self.assertEqual(articles[0]["link"], "https://mp.weixin.qq.com/s/abc_def")
        self.assertEqual(articles[0]["update_time"], 1792303881)

    def test_unrecognized_list_response_is_not_treated_as_empty_history(self):
        with self.assertRaises(WereadMPAPIError):
            parse_mp_articles({"errCode": 0})

    def test_parse_mp_articles_classifies_rate_limit(self):
        with self.assertRaises(WereadMPAPIError) as caught:
            parse_mp_articles({"errCode": -2041, "errMsg": "request blocked"})

        self.assertEqual(caught.exception.code, -2041)
        self.assertFalse(caught.exception.retriable)

    def test_extract_mp_content_returns_article_body(self):
        html = """
        <html><body>
          <div id="js_content"><p>Hello</p><script>bad()</script></div>
        </body></html>
        """

        self.assertEqual(extract_mp_content(html), '<p>Hello</p>')

    def test_build_mp_url_rejects_missing_original_id(self):
        self.assertEqual(build_mp_url(""), "")

    def test_build_mp_url_converts_weread_tilde_to_underscore(self):
        # 实例：阿信漫谈《再谈定投金额》，微信读书 ID 里 '~' 应为 '_'，
        # 原样保留会导致微信返回"参数错误"
        self.assertEqual(
            build_mp_url("ySsAk3lUFT0Wh~9IZCtr-g"),
            "https://mp.weixin.qq.com/s/ySsAk3lUFT0Wh_9IZCtr-g",
        )

    def test_build_mp_link_from_review_id_converts_tilde_and_strips_prefix(self):
        review_id = "MP_WXS_3990360005_ySsAk3lUFT0Wh~9IZCtr-g"
        self.assertEqual(
            build_mp_link_from_review_id(review_id, "MP_WXS_3990360005"),
            "https://mp.weixin.qq.com/s/ySsAk3lUFT0Wh_9IZCtr-g",
        )
        self.assertEqual(
            build_mp_link_from_review_id(review_id),
            "https://mp.weixin.qq.com/s/ySsAk3lUFT0Wh_9IZCtr-g",
        )


class WereadMpRequestTest(unittest.TestCase):
    def make_collector(self):
        collector = object.__new__(MpsWereadMP)
        collector._weread_cookies = "wr_vid=1; wr_skey=skey"
        collector._weread_accounts = []
        collector._current_account_idx = 0
        collector._weread_ticket = "ticket-value"
        collector.user_agent = "test-agent"
        collector.proxy_enabled = False
        collector.http_proxy_url = ""
        return collector

    def make_collector_with_vid(self):
        collector = self.make_collector()
        collector._weread_vid = "123"
        return collector

    @patch("core.wx.model.weread.MpsWeread._get_shelf_books")
    def test_ensure_mp_on_shelf_skips_when_already_present(self, shelf):
        shelf.return_value = [{"book_id": "MP_WXS_1", "title": "t"}]
        collector = self.make_collector_with_vid()

        ok, detail = collector.ensure_mp_on_shelf("MP_WXS_1", "Name")

        self.assertTrue(ok)
        self.assertEqual(detail, "已在书架")

    @patch("core.wx.model.weread.MpsWeread._weread_post")
    @patch("core.wx.model.weread.MpsWeread._get_shelf_books")
    def test_ensure_mp_on_shelf_adds_missing_mp(self, shelf, post):
        shelf.return_value = [{"book_id": "MP_WXS_2", "title": "other"}]
        post.return_value = {"errCode": 0, "errMsg": ""}
        collector = self.make_collector_with_vid()

        ok, detail = collector.ensure_mp_on_shelf("MP_WXS_1", "Name")

        self.assertTrue(ok)
        self.assertIn("已自动添加", detail)
        post.assert_called_once()
        url, kwargs = post.call_args
        self.assertIn("/web/shelf/add", url[0])
        self.assertEqual(kwargs["json_data"], {"bookIds": ["MP_WXS_1"]})

    @patch("core.wx.model.weread.MpsWeread._weread_post")
    @patch("core.wx.model.weread.MpsWeread._get_shelf_books")
    def test_ensure_mp_on_shelf_reports_login_expired(self, shelf, post):
        shelf.return_value = [{"book_id": "MP_WXS_2", "title": "other"}]
        post.return_value = {"errCode": -2012, "errMsg": "login expired"}
        collector = self.make_collector_with_vid()

        ok, detail = collector.ensure_mp_on_shelf("MP_WXS_1", "Name")

        self.assertFalse(ok)
        self.assertIn("登录态失效", detail)

    def test_ensure_mp_on_shelf_skips_non_mp(self):
        collector = self.make_collector_with_vid()

        ok, detail = collector.ensure_mp_on_shelf("3300008485", "Book")

        self.assertTrue(ok)
        self.assertIn("跳过", detail)

    @patch("requests.get")
    def test_article_list_request_uses_app_endpoint_and_access_token(self, get):
        response = Mock(status_code=200)
        response.json.return_value = {"reviews": []}
        get.return_value = response
        collector = self.make_collector()

        payload = collector._get_mp_articles_page("MP_WXS_1", offset=20)

        self.assertEqual(payload, {"reviews": []})
        _, kwargs = get.call_args
        self.assertEqual(get.call_args.args[0], "https://i.weread.qq.com/book/articles")
        self.assertEqual(
            kwargs["params"],
            {"bookId": "MP_WXS_1", "offset": 20, "count": 20, "synckey": 0},
        )
        self.assertEqual(kwargs["headers"]["accessToken"], "skey")
        self.assertEqual(kwargs["headers"]["vid"], "1")
        self.assertNotIn("x-wr-ticket", kwargs["headers"])

    @patch("requests.get")
    def test_article_list_allows_missing_ticket(self, get):
        response = Mock(status_code=200)
        response.json.return_value = {"reviews": []}
        get.return_value = response
        collector = self.make_collector()
        collector._weread_ticket = ""

        payload = collector._get_mp_articles_page("MP_WXS_1", offset=0)

        self.assertEqual(payload, {"reviews": []})
        self.assertNotIn("x-wr-ticket", get.call_args.kwargs["headers"])

    @patch("core.config.Config")
    @patch("driver.weread_qr.os.path.exists", return_value=True)
    def test_qr_login_keeps_access_token_separate_from_web_cookie(self, _exists, config_class):
        from driver.weread_qr import WereadQRLogin
        login = object.__new__(WereadQRLogin)
        login._build_cookie_dict = Mock(return_value={"wr_vid": "1", "wr_skey": "web-cookie"})
        login._verify_cookies = Mock(return_value=True)
        config_class.return_value.get.return_value = {"accounts": []}

        login._save_cookies_to_lic({"vid": "1", "accessToken": "original-token"})

        saved = config_class.return_value.set.call_args.args[1]["accounts"][0]
        self.assertEqual(saved["access_token"], "original-token")
        self.assertIn("wr_skey=web-cookie", saved["cookie"])

    def test_app_headers_use_original_qr_access_token(self):
        collector = self.make_collector()
        collector._weread_access_token = "qr-access-token"
        self.assertEqual(collector._weread_app_headers()["accessToken"], "qr-access-token")
        collector._weread_cookies += "; accessToken=app-token"
        self.assertEqual(collector._weread_app_headers()["accessToken"], "app-token")

    @patch("requests.get")
    def test_auth_error_keeps_original_reason_instead_of_claiming_quota(self, get):
        get.return_value = Mock(status_code=200)
        get.return_value.json.return_value = {"errcode": -2012, "errmsg": "登录超时"}
        collector = self.make_collector()

        with self.assertRaises(WereadMPAPIError) as caught:
            collector._get_mp_articles_page("MP_WXS_1")

        self.assertEqual(caught.exception.code, -2012)
        self.assertIn("登录超时", str(caught.exception))
        self.assertNotIn("配额", str(caught.exception))

    @patch("requests.get")
    def test_failover_rebuilds_app_credentials_for_next_account(self, get):
        rejected = Mock(status_code=401)
        accepted = Mock(status_code=200)
        accepted.json.return_value = {"reviews": []}
        get.side_effect = [rejected, accepted]
        collector = self.make_collector()
        collector._weread_accounts = [
            {"cookie": "wr_vid=1; wr_skey=skey"},
            {"cookie": "wr_vid=2; wr_skey=web2", "vid": "2", "access_token": "app2"},
        ]

        self.assertEqual(collector._get_mp_articles_page("MP_WXS_1"), {"reviews": []})
        headers = get.call_args.kwargs["headers"]
        self.assertEqual(headers["accessToken"], "app2")
        self.assertEqual(headers["vid"], "2")
        self.assertEqual(get.call_count, 2)

    @patch("requests.get")
    def test_all_accounts_rejected_preserves_service_error(self, get):
        rejected = Mock(status_code=200)
        rejected.json.return_value = {"errcode": "-2041", "errlog": "request blocked"}
        get.return_value = rejected
        collector = self.make_collector()
        collector._weread_accounts = [
            {"cookie": "wr_vid=1; wr_skey=one", "vid": "1"},
            {"cookie": "wr_vid=2; wr_skey=two", "vid": "2"},
        ]

        with self.assertRaises(WereadMPAPIError) as caught:
            collector._get_mp_articles_page("MP_WXS_1")

        self.assertEqual(caught.exception.code, -2041)
        self.assertIn("request blocked", str(caught.exception))
        self.assertIn("2 个账号", str(caught.exception))
        self.assertEqual(get.call_count, 2)

    @patch("requests.get")
    def test_content_request_extracts_official_article_html(self, get):
        response = Mock(
            status_code=200,
            text='<html><div id="js_content"><p>Full text</p></div></html>',
        )
        get.return_value = response
        collector = self.make_collector()

        content = collector._get_mp_content("MP_WXS_1_review-1")

        self.assertEqual(content, "<p>Full text</p>")
        self.assertEqual(get.call_args.args[0], "https://weread.qq.com/web/mp/content")
        self.assertEqual(
            get.call_args.kwargs["params"],
            {"reviewId": "MP_WXS_1_review-1"},
        )


class WereadMpCollectorTest(unittest.TestCase):
    def make_collector(self):
        collector = object.__new__(MpsWereadMP)
        collector.articles = []
        collector.aids = []
        collector.start_time = None
        collector.Gather_Content = False
        collector._weread_cookies = "wr_vid=1; wr_skey=skey"
        collector._weread_accounts = []
        collector._current_account_idx = 0
        collector._weread_ticket = "ticket-value"
        collector._weread_vid = "1"
        collector._weread_name = "tester"
        collector._cookies = {}
        collector.user_agent = "test-agent"
        collector.proxy_enabled = False
        collector.http_proxy_url = ""
        collector.get_token = Mock()
        collector._load_weread_auth = Mock()
        collector.update_mps = Mock()
        collector._get_feed_update_time = Mock(return_value=0)
        collector._get_content_interval = Mock(return_value=0)
        collector._get_page_interval = Mock(return_value=0)
        collector._is_article_gathered = Mock(return_value=False)
        collector.ensure_mp_on_shelf = Mock(return_value=(True, "already on shelf"))
        return collector

    @patch("core.wx.base.RSS.clear_cache")
    @patch("core.wx.base.setStatus")
    def test_get_articles_collects_mp_article_and_advances_after_success(
        self, _set_status, _clear_cache
    ):
        collector = self.make_collector()
        collector._get_mp_articles_page = Mock(return_value={
            "reviews": [{
                "subReviews": [{
                    "review": {
                        "reviewId": "MP_WXS_1_review-1",
                        "createTime": 1778580001,
                        "mpInfo": {
                            "title": "Article title",
                            "content": "Summary",
                            "time": 1778580002,
                            "originalId": "abc~def",
                            "pic_url": "https://example.test/cover.jpg",
                        },
                    }
                }]
            }]
        })
        collector._get_mp_content = Mock(return_value="<p>Full text</p>")
        saved = []

        collector.get_Articles(
            faker_id="legacy-fake-id",
            Mps_id="MP_WXS_1",
            Mps_title="Feed title",
            CallBack=lambda article: saved.append(article) or True,
            MaxPage=1,
            Gather_Content=True,
            interval=0,
        )

        collector._get_mp_articles_page.assert_called_once_with("MP_WXS_1", offset=0)
        self.assertEqual(saved[0]["url"], "https://mp.weixin.qq.com/s/abc_def")
        self.assertEqual(saved[0]["content"], "<p>Full text</p>")
        self.assertEqual(saved[0]["publish_time"], 1778580002)
        self.assertEqual(collector.all_count(), 1)
        collector.update_mps.assert_called_once()
        updated_feed = collector.update_mps.call_args.args[1]
        self.assertEqual(updated_feed.update_time, 1778580002)

    @patch("core.wx.base.RSS.clear_cache")
    def test_get_articles_does_not_advance_after_list_failure(self, _clear_cache):
        collector = self.make_collector()
        collector._get_mp_articles_page = Mock(
            side_effect=WereadMPAPIError(-2041, "blocked", retriable=False)
        )
        collector._get_mp_cover = Mock(
            side_effect=WereadMPAPIError(-2012, "login expired")
        )

        with self.assertRaises(WereadMPAPIError):
            collector.get_Articles(
                Mps_id="MP_WXS_1",
                Mps_title="Feed title",
                CallBack=lambda article: True,
                MaxPage=1,
                interval=0,
            )

        collector.update_mps.assert_not_called()

    @patch("driver.wxarticle.Web.get_article_content", return_value={"publish_time": 1792303881})
    @patch("core.wx.base.RSS.clear_cache")
    def test_list_failure_falls_back_to_cover_latest_article(self, _clear_cache, fetch_article):
        collector = self.make_collector()
        collector._get_mp_articles_page = Mock(
            side_effect=WereadMPAPIError(-2041, "blocked", retriable=False)
        )
        collector._get_mp_cover = Mock(return_value={
            "reviewId": "MP_WXS_1_latest",
            "title": "Latest",
            "pic": "https://example.test/pic.jpg",
        })
        saved = []

        collector.get_Articles(
            Mps_id="MP_WXS_1",
            Mps_title="Feed title",
            CallBack=lambda article: saved.append(article) or True,
            MaxPage=1,
            interval=0,
        )

        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["title"], "Latest")
        self.assertEqual(saved[0]["url"], "https://mp.weixin.qq.com/s/latest")
        self.assertEqual(saved[0]["publish_time"], 1792303881)
        fetch_article.assert_called_once_with("https://mp.weixin.qq.com/s/latest")
        collector.update_mps.assert_called_once()

    @patch("driver.wxarticle.Web.get_article_content", return_value={"publish_time": 0})
    def test_cover_missing_publish_time_does_not_save(self, _fetch_article):
        collector = self.make_collector()
        collector._get_mp_cover = Mock(return_value={
            "reviewId": "MP_WXS_1_latest", "title": "Latest",
        })
        callback = Mock()

        with self.assertRaises(WereadMPAPIError):
            collector._collect_latest_via_cover(
                "MP_WXS_1", "MP_WXS_1", "Feed title", False, CallBack=callback,
            )

        callback.assert_not_called()

    @patch("core.wx.base.RSS.clear_cache")
    def test_app_history_pages_are_saved_without_cover_fallback(self, _clear_cache):
        collector = self.make_collector()
        collector._is_article_gathered = Mock(return_value=False)
        collector.ensure_mp_on_shelf = Mock(return_value=(True, "already on shelf"))
        def page(start, count):
            return {"reviews": [{"review": {
                "reviewId": f"MP_WXS_1_{i}", "createTime": 1792303881 - i,
                "mpInfo": {"title": str(i), "originalId": str(i), "time": 1792303881 - i},
            }} for i in range(start, start + count)]}
        collector._get_mp_articles_page = Mock(side_effect=[page(0, 20), page(20, 3), page(23, 0)])
        collector._get_mp_cover = Mock()
        saved = []

        collector.get_Articles(
            Mps_id="MP_WXS_1", Mps_title="History", MaxPage=3,
            CallBack=lambda article: saved.append(article) or True,
        )

        self.assertEqual(len(saved), 23)
        self.assertEqual(saved[0]["publish_time"], 1792303881)
        self.assertEqual(saved[-1]["publish_time"], 1792303859)
        self.assertEqual(collector._get_mp_articles_page.call_args_list, [
            call("MP_WXS_1", offset=0), call("MP_WXS_1", offset=20), call("MP_WXS_1", offset=23),
        ])
        collector._get_mp_cover.assert_not_called()

    def test_requested_history_pages_continue_past_known_latest_article(self):
        collector = self.make_collector()
        collector._get_feed_update_time = Mock(return_value=1792303881)
        collector._get_catchup_page_limit = Mock(return_value=20)
        collector._is_article_gathered = Mock(side_effect=lambda mp, aid: aid.endswith("latest"))
        collector._get_mp_articles_page = Mock(side_effect=[
            {"reviews": [{"review": {"reviewId": "MP_WXS_1_latest", "mpInfo": {"time": 1792303881}}}]},
            {"reviews": [{"review": {"reviewId": "MP_WXS_1_older", "mpInfo": {"time": 1792200000}}}]},
        ])
        saved = []

        latest = collector._collect_via_article_list(
            "MP_WXS_1", "MP_WXS_1", "History", False, 0, 2,
            CallBack=lambda article: saved.append(article) or True,
        )

        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["publish_time"], 1792200000)
        self.assertEqual(latest, 1792303881)
        self.assertEqual(collector._get_mp_articles_page.call_count, 2)

    def test_start_page_is_converted_to_article_offset(self):
        collector = self.make_collector()
        collector._get_mp_articles_page = Mock(return_value={"reviews": []})
        collector._collect_via_article_list("MP_WXS_1", "MP_WXS_1", "History", False, 2, 3)
        collector._get_mp_articles_page.assert_called_once_with("MP_WXS_1", offset=40)

    @patch("core.wx.base.RSS.clear_cache")
    def test_pagination_counts_groups_not_articles(self, _clear_cache):
        collector = self.make_collector()
        collector._get_mp_articles_page = Mock(side_effect=[
            {
                "reviews": [
                    {"subReviews": [{} for _ in range(20)]},
                ],
            },
            {"reviews": []},
        ])

        collector.get_Articles(
            Mps_id="MP_WXS_1",
            Mps_title="Feed title",
            CallBack=lambda article: True,
            MaxPage=2,
            interval=0,
        )

        self.assertEqual(
            collector._get_mp_articles_page.call_args_list,
            [
                unittest.mock.call("MP_WXS_1", offset=0),
                unittest.mock.call("MP_WXS_1", offset=1),
            ],
        )

    @patch("core.wx.base.RSS.clear_cache")
    def test_scheduled_run_catches_up_to_previous_update_time(self, _clear_cache):
        collector = self.make_collector()
        collector._get_feed_update_time.return_value = 1778580001
        collector._get_catchup_page_limit = Mock(return_value=3)
        collector._is_article_gathered = Mock(
            side_effect=lambda mp_id, aid: aid == "MP_WXS_1_old"
        )
        collector._get_mp_articles_page = Mock(side_effect=[
            {
                "reviews": [{
                    "subReviews": [{
                        "review": {
                            "reviewId": "MP_WXS_1_new",
                            "createTime": 1778580003,
                            "mpInfo": {"title": "New", "originalId": "new"},
                        }
                    }]
                }]
            },
            {
                "reviews": [{
                    "subReviews": [{
                        "review": {
                            "reviewId": "MP_WXS_1_old",
                            "createTime": 1778580001,
                            "mpInfo": {"title": "Old", "originalId": "old"},
                        }
                    }]
                }]
            },
        ])

        collector.get_Articles(
            Mps_id="MP_WXS_1",
            Mps_title="Feed title",
            CallBack=lambda article: True,
            MaxPage=1,
            interval=0,
        )

        self.assertEqual(
            collector._get_mp_articles_page.call_args_list,
            [
                unittest.mock.call("MP_WXS_1", offset=0),
                unittest.mock.call("MP_WXS_1", offset=1),
            ],
        )
        collector.update_mps.assert_called_once()

    @patch("core.wx.base.RSS.clear_cache")
    def test_cover_era_update_time_does_not_block_backfill(self, _clear_cache):
        """cover 模式写入的 update_time 是抓取时间（晚于文章发布时间），
        不能因此跳过 cover 时代漏采的文章；以入库记录为停止边界。"""
        collector = self.make_collector()
        collector._get_feed_update_time.return_value = 1778700000
        collector._is_article_gathered = Mock(
            side_effect=lambda mp_id, aid: aid == "MP_WXS_1_latest"
        )
        collector._get_mp_articles_page = Mock(return_value={
            "reviews": [{
                "subReviews": [
                    {
                        "review": {
                            "reviewId": "MP_WXS_1_latest",
                            "createTime": 1778580003,
                            "mpInfo": {"title": "Latest", "originalId": "latest"},
                        }
                    },
                    {
                        "review": {
                            "reviewId": "MP_WXS_1_missed",
                            "createTime": 1778580002,
                            "mpInfo": {"title": "Missed", "originalId": "missed"},
                        }
                    },
                ]
            }]
        })
        saved = []

        collector.get_Articles(
            Mps_id="MP_WXS_1",
            Mps_title="Feed title",
            CallBack=lambda article: saved.append(article) or True,
            MaxPage=1,
            interval=0,
        )

        self.assertEqual([a["title"] for a in saved], ["Missed"])
        collector.update_mps.assert_called_once()

    @patch("core.wx.base.RSS.clear_cache")
    def test_incomplete_catchup_does_not_mark_feed_synced(self, _clear_cache):
        collector = self.make_collector()
        collector._get_feed_update_time.return_value = 1778580001
        collector._get_catchup_page_limit = Mock(return_value=1)
        collector._get_mp_articles_page = Mock(return_value={
            "reviews": [{
                "subReviews": [{
                    "review": {
                        "reviewId": "MP_WXS_1_new",
                        "createTime": 1778580003,
                        "mpInfo": {"title": "New", "originalId": "new"},
                    }
                }]
            }]
        })

        with self.assertRaises(WereadMPAPIError) as caught:
            collector.get_Articles(
                Mps_id="MP_WXS_1",
                Mps_title="Feed title",
                CallBack=lambda article: True,
                MaxPage=1,
                interval=0,
            )

        self.assertEqual(caught.exception.code, "backlog_incomplete")
        collector.update_mps.assert_not_called()

    @patch("core.wx.model.weread_mp.time.sleep")
    @patch("core.wx.base.RSS.clear_cache")
    def test_fulltext_requests_are_throttled_between_articles(self, _clear_cache, sleep):
        collector = self.make_collector()
        collector._get_content_interval.return_value = 2
        collector._get_mp_articles_page = Mock(return_value={
            "reviews": [{
                "subReviews": [
                    {
                        "review": {
                            "reviewId": "MP_WXS_1_a",
                            "createTime": 1778580002,
                            "mpInfo": {"title": "A", "originalId": "a"},
                        }
                    },
                    {
                        "review": {
                            "reviewId": "MP_WXS_1_b",
                            "createTime": 1778580001,
                            "mpInfo": {"title": "B", "originalId": "b"},
                        }
                    },
                ]
            }]
        })
        collector._get_mp_content = Mock(return_value="<p>Full text</p>")

        collector.get_Articles(
            Mps_id="MP_WXS_1",
            Mps_title="Feed title",
            CallBack=lambda article: True,
            MaxPage=1,
            Gather_Content=True,
            interval=0,
        )

        sleep.assert_called_once_with(2)

    @patch("core.wx.base.RSS.clear_cache")
    def test_fulltext_failure_is_not_saved_or_marked_synced(self, _clear_cache):
        collector = self.make_collector()
        collector._get_mp_articles_page = Mock(return_value={
            "reviews": [{
                "subReviews": [{
                    "review": {
                        "reviewId": "MP_WXS_1_review-1",
                        "createTime": 1778580001,
                        "mpInfo": {
                            "title": "Article title",
                            "originalId": "abc~def",
                        },
                    }
                }]
            }]
        })
        collector._get_mp_content = Mock(
            side_effect=WereadMPAPIError(503, "content unavailable")
        )
        saved = []

        with self.assertRaises(WereadMPAPIError) as caught:
            collector.get_Articles(
                Mps_id="MP_WXS_1",
                Mps_title="Feed title",
                CallBack=lambda article: saved.append(article) or True,
                MaxPage=1,
                Gather_Content=True,
                interval=0,
            )

        self.assertEqual(caught.exception.code, "content_incomplete")
        self.assertEqual(saved, [])
        collector.update_mps.assert_not_called()

if __name__ == "__main__":
    unittest.main()
