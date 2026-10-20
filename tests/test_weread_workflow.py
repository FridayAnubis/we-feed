import unittest
from unittest.mock import Mock, patch

from core.weread_client import WereadClient, WereadAPIError, _cooldowns
from core.wx.model import test_weread_mp as helpers
from core.wx.model.weread_mp import MpsWereadMP, mp_book_id
from tests.test_weread_client import response


def review(token, timestamp=1778580002):
    return {"reviewId": f"MP_WXS_123_{token}", "title": token, "createTime": timestamp}


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        _cooldowns.clear()
        self.collector = helpers.WereadMpCollectorTest().make_collector()
        del self.collector._app_json
        self.collector._get_catchup_page_limit = Mock(return_value=3)
        self.session = Mock()
        self.session.post.return_value = response()
        self.client = WereadClient([{"vid": "123", "access_token": "token"}], session=self.session)
        self.collector._weread_client = self.client
        self.callbacks = Mock(return_value=True)
        self.done = Mock()
        self.routes = {
            "/shelf/sync": response(books=[]), "/mp/list": response(data=[]),
            "/mp/cover": response(reviewId="MP_WXS_123_latest", title="latest"),
        }
        self.session.get.side_effect = self.route
        self.clear = patch("core.wx.base.RSS.clear_cache").start()
        self.addCleanup(patch.stopall)

    def route(self, url, **kwargs):
        path = url.split(".com")[-1]
        value = self.routes.get(path)
        if value is None:
            raise AssertionError(f"Unexpected request: {path}")
        if isinstance(value, list):
            return value.pop(0)
        return value

    def run_collection(self):
        self.collector.get_Articles(Mps_id="MP_WXS_123", Mps_title="test",
                                    CallBack=self.callbacks, Over_CallBack=self.done)

    def test_complete_workflow_activates_prefetches_cover_and_paginates(self):
        self.routes["/mp/chapters"] = [
            response(data=[review("latest"), review("older")], hasMore=1),
            response(data=[review("oldest")], hasMore=0),
        ]
        self.run_collection()
        self.assertEqual(self.callbacks.call_count, 3)
        self.done.assert_called_once()
        self.collector.update_mps.assert_called_once()
        chapter_calls = [c for c in self.session.get.call_args_list if c.args[0].endswith("/mp/chapters")]
        self.assertEqual([c.kwargs["params"]["offset"] for c in chapter_calls], [0, 2])
        self.assertEqual(self.session.post.call_count, 1)
        self.assertEqual(self.collector.history_source, "chapters")
        self.collector.ensure_mp_on_shelf.assert_not_called()

    def test_chapters_failure_uses_backup_cursor_and_keeps_cover_deduplicated(self):
        self.routes["/mp/chapters"] = response(404, errmsg="unavailable")
        self.routes["/article/list"] = [
            response(reviews=[{"review": review("latest")}, {"review": review("older")}],
                     hasMore=1, synckey=100),
            response(reviews=[{"review": review("oldest")}], hasMore=0, synckey=200),
        ]
        self.run_collection()
        self.assertEqual(self.callbacks.call_count, 3)
        backup = [c for c in self.session.get.call_args_list if c.args[0].endswith("/article/list")]
        self.assertEqual([c.kwargs["params"]["synckey"] for c in backup], [0, 100])
        self.assertEqual(self.collector.history_source, "article/list")
        self.assertIn("article/list", self.collector.history_warning)

    def test_mixed_known_page_does_not_stop_historical_backfill(self):
        self.collector._get_feed_update_time.return_value = 1778580002
        self.collector._is_article_gathered.side_effect = lambda mp_id, rid: rid.endswith("_latest")
        self.routes["/mp/chapters"] = [
            response(data=[review("latest"), review("missed")], hasMore=1),
            response(data=[review("older")], hasMore=0),
        ]
        self.run_collection()
        self.assertEqual(self.callbacks.call_count, 2)

    def test_partial_failure_does_not_fall_back_to_cover_or_mark_feed_synced(self):
        self.routes["/mp/chapters"] = [
            response(data=[review("latest")], hasMore=1), response(503),
        ]
        self.routes["/article/list"] = response(503)
        with self.assertRaises(WereadAPIError):
            self.run_collection()
        self.assertEqual(self.callbacks.call_count, 1)
        self.collector.update_mps.assert_not_called()
        self.done.assert_called_once()

    def test_stalled_backup_cursor_is_not_success(self):
        self.routes["/mp/chapters"] = response(404)
        self.routes["/article/list"] = [
            response(reviews=[review("latest")], synckey=100, hasMore=1),
            response(reviews=[review("older")], synckey=100, hasMore=1),
        ]
        with self.assertRaises(WereadAPIError) as error:
            self.run_collection()
        self.assertEqual(error.exception.code, "stalled_cursor")
        self.collector.update_mps.assert_not_called()

    def test_total_history_failure_retains_explicit_cover_only_result(self):
        self.routes["/mp/chapters"] = response(503)
        self.routes["/article/list"] = response(503)
        self.run_collection()
        self.assertEqual(self.callbacks.call_count, 1)
        self.assertEqual(self.collector.history_source, "cover")
        self.assertIn("仅获取最新一篇", self.collector.history_warning)

    def test_review_single_supplies_original_link_and_publication_time(self):
        self.routes["/mp/chapters"] = response(data=[review("latest", 0)], hasMore=0)
        self.routes["/review/single"] = response(review={
            **review("latest"), "mpInfo": {"doc_url": "https://mp.weixin.qq.com/s/original", "time": 1778580001},
        })
        self.run_collection()
        item = self.callbacks.call_args.args[0]
        self.assertEqual(item["url"], "https://mp.weixin.qq.com/s/original")
        self.assertEqual(item["publish_time"], 1778580001)

    def test_fakeid_conversion_keeps_existing_feed_identity(self):
        self.assertEqual(mp_book_id("MTIz"), "MP_WXS_123")
        self.routes["/mp/chapters"] = response(data=[review("latest")], hasMore=0)
        self.collector.get_Articles(faker_id="MTIz", Mps_id="existing-feed", CallBack=self.callbacks)
        self.assertEqual(self.callbacks.call_args.args[0]["mp_id"], "existing-feed")

    def test_missing_metadata_does_not_block_other_articles_on_page(self):
        self.routes["/mp/chapters"] = response(data=[review("broken", 0), review("latest")], hasMore=0)
        self.routes["/review/single"] = response(404)
        self.collector._get_original_article.return_value = {}
        with self.assertRaises(WereadAPIError) as error:
            self.run_collection()
        self.assertEqual(error.exception.code, "invalid_publish_time")
        self.assertEqual(self.callbacks.call_count, 1)
        self.collector.update_mps.assert_not_called()

    def test_failed_database_callback_does_not_advance_sync(self):
        self.routes["/mp/chapters"] = response(data=[review("latest")], hasMore=0)
        self.callbacks.return_value = False
        with self.assertRaises(WereadAPIError) as error:
            self.run_collection()
        self.assertEqual(error.exception.code, "save_failed")
        self.collector.update_mps.assert_not_called()


class ContentWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.collector = helpers.WereadMpCollectorTest().make_collector()
        del self.collector._get_original_article

    @patch("requests.get")
    def test_public_html_is_preferred_and_lazy_images_are_restored(self, get):
        raw = response()
        raw._content = b'<script>var ct="1778580002";</script><div id="js_content"><p>Body</p><img data-src="https://img.test/a.jpg"></div>'
        get.return_value = raw
        content = self.collector._get_mp_content("MP_WXS_123_article")
        self.assertIn('src="https://img.test/a.jpg"', content)
        self.assertNotIn("data-src", content)
        self.assertNotIn("Cookie", get.call_args.kwargs["headers"])

    @patch("driver.wxarticle.Web.get_article_content")
    @patch("requests.get")
    def test_web_content_then_browser_fallback(self, get, browser):
        blocked = response(403)
        body = response()
        body._content = b'<div id="js_content"><p>WeRead body</p></div>'
        get.side_effect = [blocked, body]
        content = self.collector._get_mp_content("MP_WXS_123_article")
        self.assertIn("WeRead body", content)
        browser.assert_not_called()
        get.side_effect = None
        get.return_value = blocked
        browser.return_value = {"content": "<p>Browser body</p>", "publish_time": 1778580002}
        self.assertIn("Browser body", self.collector._get_mp_content("MP_WXS_123_other"))
        browser.assert_called_once()


class QRWorkflowTest(unittest.TestCase):
    def setUp(self):
        from driver.weread_qr import WereadQRLogin
        self.login = object.__new__(WereadQRLogin)
        self.login._session = Mock()
        self.login._uid = "test"
        self.login._build_cookie_dict = Mock(return_value={"wr_vid": "123", "wr_skey": "short"})

    def test_short_login_token_and_refresh_token_are_separate(self):
        self.login._session.get.return_value = response(
            data={"succeed": True, "vid": "123", "accessToken": "short", "refreshToken": "long-refresh"},
        )
        result = self.login._check_login_status()
        self.assertTrue(result["succeed"])
        self.assertEqual(result["accessToken"], "short")
        self.assertEqual(result["wr_skey"], "short")
        self.assertEqual(result["refreshToken"], "long-refresh")

    @patch("core.weread_client.WereadClient")
    def test_scraping_restriction_does_not_reject_successful_qr_login(self, client):
        self.login._check_login_status = Mock(return_value={
            "succeed": True, "vid": "123", "accessToken": "short",
            "wr_skey": "short", "refreshToken": "long-refresh",
        })
        self.login._save_cookies_to_lic = Mock(return_value=True)
        client.return_value.prepare.return_value = None
        callback = Mock()
        self.login._poll_once(callback)
        self.assertTrue(self.login._logged_in)
        saved = self.login._save_cookies_to_lic.call_args.args[0]
        self.assertEqual(saved["cookies"]["wr_skey"], "short")
        self.assertIn("deviceId", saved)
        callback.assert_called_once()

    @patch("core.config.Config")
    def test_save_retains_refresh_device_and_skey(self, config):
        config.return_value.get.return_value = {"accounts": [{"vid": "456", "cookie": "other"}]}
        result = {"vid": "123", "accessToken": "short", "refreshToken": "refresh",
                  "wr_skey": "short", "deviceId": "device-123",
                  "cookies": {"wr_vid": "123", "wr_skey": "short"}}
        self.assertTrue(self.login._save_cookies_to_lic(result))
        saved = config.return_value.set.call_args.args[1]["accounts"]
        self.assertEqual(saved[0]["vid"], "456")
        self.assertEqual(saved[1]["access_token"], "short")
        self.assertEqual(saved[1]["refresh_token"], "refresh")
        self.assertEqual(saved[1]["device_id"], "device-123")


class HistoryPriorityTest(unittest.TestCase):
    @patch("core.wx.model.history_first.MpsWereadMP")
    def test_success_does_not_call_old_collector_and_completes_once(self, cls):
        from core.wx.model.history_first import MpsHistoryFirst
        mobile, fallback, done = cls.return_value, Mock(), Mock()
        mobile._weread_cookies = "wr_vid=123; wr_skey=token"
        mobile.articles = [{"id": "article"}]
        wrapper = MpsHistoryFirst(fallback)
        wrapper.get_Articles(faker_id="MTIz", Mps_id="existing", Over_CallBack=done)
        fallback.get_Articles.assert_not_called()
        done.assert_called_once_with(mobile.articles)
        self.assertEqual(wrapper.articles, mobile.articles)

    @patch("core.wx.model.history_first.MpsWereadMP")
    def test_total_failure_uses_old_collector_without_looping_back_to_mobile(self, cls):
        from core.wx.model.history_first import MpsHistoryFirst, history_attempted
        mobile, fallback, done = cls.return_value, Mock(), Mock()
        mobile._weread_cookies = "wr_vid=123; wr_skey=token"
        mobile.response_valid = False
        mobile.get_Articles.side_effect = WereadAPIError(401, "expired")
        def old_collect(**kwargs):
            self.assertTrue(history_attempted.get())
            kwargs["Over_CallBack"]([])
        fallback.get_Articles.side_effect = old_collect
        MpsHistoryFirst(fallback).get_Articles(faker_id="MTIz", Over_CallBack=done)
        fallback.get_Articles.assert_called_once()
        done.assert_called_once_with([])
        self.assertFalse(history_attempted.get())

    @patch("core.wx.model.history_first.MpsWereadMP")
    def test_partial_success_is_not_replayed_by_old_collector(self, cls):
        from core.wx.model.history_first import MpsHistoryFirst
        mobile, fallback, done = cls.return_value, Mock(), Mock()
        mobile._weread_cookies = "wr_vid=123; wr_skey=token"
        mobile.response_valid = True
        mobile.articles = [{"id": "saved"}]
        mobile.get_Articles.side_effect = WereadAPIError(503, "page failed")
        with self.assertRaises(WereadAPIError):
            MpsHistoryFirst(fallback).get_Articles(faker_id="MTIz", Over_CallBack=done)
        fallback.get_Articles.assert_not_called()
        done.assert_called_once_with(mobile.articles)

    @patch("core.wx.model.history_first.MpsWereadMP")
    def test_no_weread_credentials_preserves_old_collector(self, cls):
        from core.wx.model.history_first import MpsHistoryFirst
        cls.return_value._weread_cookies = ""
        fallback = Mock()
        MpsHistoryFirst(fallback).get_Articles(faker_id="MTIz")
        fallback.get_Articles.assert_called_once()
        cls.return_value.get_Articles.assert_not_called()


class NativeKeepaliveTest(unittest.TestCase):
    @patch("core.weread_keepalive._refresh_account_cookie")
    @patch("core.weread_client.WereadClient")
    def test_keepalive_prefers_mobile_and_never_rewrites_stale_document(self, client, browser):
        from core.weread_keepalive import run_keepalive
        import tempfile
        from pathlib import Path
        import yaml
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wx.lic"
            text = yaml.safe_dump({"weread_data": {"accounts": [
                {"vid": "123", "cookie": "wr_vid=123; wr_skey=token", "refresh_token": "refresh"},
            ]}})
            path.write_text(text)
            self.assertTrue(run_keepalive(str(path)))
            client.return_value.json.assert_called_once_with("/shelf/sync", {"synckey": 0}, prepare=True)
            browser.assert_not_called()
            self.assertEqual(path.read_text(), text)

    @patch("core.weread_cookie_refresh.request_host_refresh")
    @patch("jobs.mps.TaskQueue")
    def test_job_can_queue_without_host_cookie_preflight(self, queue, host_refresh):
        from jobs.mps import add_job
        with patch("jobs.mps.cfg.get", side_effect=lambda key, default=None: {
            "gather.model": "weread_mp", "weread.history_first": True,
        }.get(key, default)):
            add_job(feeds=[])
        host_refresh.assert_not_called()
