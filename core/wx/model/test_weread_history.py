import unittest
from unittest.mock import Mock, patch

from core.wx.model import test_weread_mp as helpers
from core.wx.model.weread_mp import WereadMPAPIError, parse_mp_articles


def response(status=200, **payload):
    result = Mock(status_code=status)
    result.json.return_value = payload
    return result


def chapter(token="article~one", timestamp=1778580002):
    return {
        "reviewId": f"MP_WXS_1_{token}",
        "title": "历史文章",
        "createTime": timestamp,
        "mpInfo": {"time": timestamp, "digest": "摘要"},
    }


class HistoryParsingTest(unittest.TestCase):
    def test_mobile_shapes_and_legacy_wrapper(self):
        for key in ("data", "chapters", "list", "reviews"):
            with self.subTest(key=key):
                articles, count = parse_mp_articles({key: [chapter()]})
                self.assertEqual(count, 1)
                self.assertEqual(articles[0]["link"], "https://mp.weixin.qq.com/s/article_one")
                self.assertEqual(articles[0]["update_time"], 1778580002)
                self.assertEqual(articles[0]["title"], "历史文章")
        articles, _ = parse_mp_articles({"reviews": [{"review": chapter()}]})
        self.assertEqual(articles[0]["aid"], "MP_WXS_1_article~one")

    def test_explicit_original_link_is_preserved(self):
        item = chapter()
        item["mpInfo"]["doc_url"] = "https://mp.weixin.qq.com/s?__biz=1&mid=2&idx=1"
        articles, _ = parse_mp_articles({"data": [item]})
        self.assertEqual(articles[0]["link"], item["mpInfo"]["doc_url"])

    def test_unknown_schema_cannot_be_treated_as_empty_history(self):
        for payload in ({}, {"data": {}}, {"data": [None]}, {"data": [{}]}):
            with self.subTest(payload=payload), self.assertRaises(WereadMPAPIError):
                parse_mp_articles(payload)


class HistoryCollectionTest(unittest.TestCase):
    def setUp(self):
        self.collector = helpers.WereadMpCollectorTest().make_collector()
        self.collector._is_article_gathered = Mock(return_value=False)
        self.collector._get_original_article.side_effect = lambda *args, **kwargs: __import__("driver.wxarticle", fromlist=["Web"]).Web.get_article_content(args[0])

    def collect(self, start_page=0, max_page=3):
        return self.collector._collect_via_article_list(
            "MP_WXS_1", "MP_WXS_1", "测试", False, start_page, max_page,
            Item_Over_CallBack=self.on_item,
        )

    def test_history_end_completes_catchup_without_an_extra_request(self):
        self.on_item = Mock()
        self.collector._get_feed_update_time.return_value = 1
        self.collector._get_catchup_page_limit = Mock(return_value=20)
        self.collector._get_mp_articles_page = Mock(return_value={
            "data": [chapter()], "hasMore": 0,
        })
        self.assertEqual(self.collect(), 1778580002)
        self.on_item.assert_called_once()
        self.collector._get_mp_articles_page.assert_called_once()

    def test_offset_uses_raw_count_and_start_page(self):
        self.on_item = Mock()
        self.collector._get_mp_articles_page = Mock(side_effect=[
            {"data": [chapter(), chapter(), chapter("second")], "hasMore": 1},
            {"data": [chapter("third")], "hasMore": 0},
        ])
        self.collect(start_page=2, max_page=4)
        offsets = [call.kwargs["offset"] for call in self.collector._get_mp_articles_page.call_args_list]
        self.assertEqual(offsets, [40, 43])
        self.assertEqual(self.on_item.call_count, 3)

    @patch("driver.wxarticle.Web.get_article_content")
    def test_missing_publication_time_uses_original_article(self, fetch):
        self.on_item = Mock()
        fetch.return_value = {"publish_time": 1778580002}
        self.collector._get_mp_articles_page = Mock(return_value={
            "data": [chapter(timestamp=0)], "hasMore": 0,
        })
        self.assertEqual(self.collect(), 1778580002)
        self.assertEqual(self.on_item.call_args.args[0]["update_time"], 1778580002)

    @patch("driver.wxarticle.Web.get_article_content", return_value={})
    def test_missing_publication_time_is_not_saved(self, fetch):
        self.on_item = Mock()
        self.collector._get_mp_articles_page = Mock(return_value={
            "data": [chapter(timestamp=0)], "hasMore": 0,
        })
        with self.assertRaises(WereadMPAPIError) as caught:
            self.collect()
        self.assertEqual(caught.exception.code, "invalid_publish_time")
        self.on_item.assert_not_called()


if __name__ == "__main__":
    unittest.main()
