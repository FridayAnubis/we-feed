import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from core.weread_client import (
    WereadClient, WereadAPIError, _cooldowns, normalize_account, persist_account,
)


def response(status=200, **data):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(data).encode()
    return result


class WereadClientTest(unittest.TestCase):
    def setUp(self):
        _cooldowns.clear()
        self.session = Mock()
        self.client = WereadClient([{
            "vid": "123", "cookie": "wr_vid=123; wr_skey=short123",
            "refresh_token": "refresh-value", "device_id": "device-123",
        }], session=self.session, proxies={"https": "http://proxy.test"})

    def test_short_access_token_is_not_replaced_with_refresh_token(self):
        account = normalize_account({"cookie": "wr_vid=123; wr_skey=abcd; wr_rt=web%40long-value"})
        self.assertEqual(account["access_token"], "abcd")
        self.assertEqual(account["refresh_token"], "web@long-value")
        self.assertEqual(account["device_id"], normalize_account(account)["device_id"])

    def test_activation_warms_shelf_and_mp_index_through_configured_proxy(self):
        self.session.post.return_value = response()
        self.session.get.return_value = response()
        self.assertTrue(self.client.activate())
        self.assertEqual([c.args[0].split(".com")[1] for c in self.session.get.call_args_list],
                         ["/shelf/sync", "/mp/list"])
        self.assertEqual(self.session.post.call_args.kwargs["proxies"], {"https": "http://proxy.test"})
        self.assertFalse(self.session.post.call_args.kwargs["allow_redirects"])

    def test_failed_activation_does_not_report_success(self):
        for result in (response(401), response(errcode=-2012), response(200, errCode=-2041)):
            self.session.post.return_value = result
            self.assertFalse(self.client.activate())
        self.session.get.assert_not_called()

    def test_401_refreshes_current_account_persists_and_retries_with_new_headers(self):
        self.session.get.side_effect = [
            response(401, errcode=-2012),
            response(), response(),  # activation warm-up
            response(data=[{"reviewId": "r"}]),
        ]
        self.session.post.side_effect = [
            response(accessToken="new-token", refreshToken="new-refresh", skey="new-skey", vid=123),
            response(),
        ]
        self.client.on_refresh = Mock()
        self.assertIn("data", self.client.json("/mp/chapters"))
        self.assertEqual(self.session.get.call_args.kwargs["headers"]["accessToken"], "new-token")
        self.assertEqual(self.session.get.call_args.kwargs["headers"]["skey"], "new-skey")
        self.assertEqual(self.session.post.call_args_list[0].kwargs["json"]["deviceId"], "device-123")
        previous, updated = self.client.on_refresh.call_args.args
        self.assertEqual(previous["access_token"], "short123")
        self.assertEqual(updated["refresh_token"], "new-refresh")
        self.assertIn("wr_rt=new-refresh", updated["cookie"])

    def test_refresh_is_attempted_only_once_and_mismatched_vid_is_rejected(self):
        self.session.get.return_value = response(401, errcode=-2012)
        self.session.post.return_value = response(accessToken="other", vid=456)
        with self.assertRaises(WereadAPIError):
            self.client.json("/mp/chapters")
        self.session.post.assert_called_once()
        self.assertEqual(self.client.account["access_token"], "short123")

    def test_499_body_is_used_for_activation_and_retry_is_bounded(self):
        self.client.accounts[0]["refresh_token"] = ""
        self.session.get.side_effect = [
            response(499, errcode=-2041, errmsg="inactive"),
            response(), response(),
            response(499, errcode=-2041, errmsg="still inactive"),
        ]
        self.session.post.return_value = response()
        with self.assertRaises(WereadAPIError) as error:
            self.client.json("/mp/chapters")
        self.assertEqual(error.exception.code, -2041)
        self.assertNotIn("配额", str(error.exception))
        self.session.post.assert_called_once()
        before = self.session.get.call_count
        with self.assertRaises(WereadAPIError):
            self.client.json("/mp/chapters")
        self.assertEqual(self.session.get.call_count, before)
        # Endpoint-specific cooldown still permits a useful alternative.
        self.session.get.side_effect = None
        self.session.get.return_value = response(reviews=[])
        self.assertEqual(self.client.json("/article/list"), {"reviews": []})

    def test_non_json_499_is_not_misdiagnosed_or_activated(self):
        raw = response(499)
        raw._content = b"<html>request blocked</html>"
        self.session.get.return_value = raw
        with self.assertRaises(WereadAPIError) as error:
            self.client.json("/mp/chapters")
        self.assertEqual(error.exception.code, 499)
        self.session.post.assert_not_called()

    def test_account_rotation_rebuilds_all_headers_and_clears_session_cookies(self):
        self.client.accounts[0]["refresh_token"] = ""
        self.client.accounts.append(normalize_account({
            "vid": "456", "access_token": "second", "skey": "second-skey",
        }))
        self.session.get.side_effect = [response(401), response(data=[])]
        self.client.json("/mp/chapters")
        headers = self.session.get.call_args.kwargs["headers"]
        self.assertEqual(headers["vid"], "456")
        self.assertEqual(headers["accessToken"], "second")
        self.assertNotIn("short123", headers["Cookie"])
        self.assertEqual(self.session.cookies.clear.call_count, 2)

    def test_refresh_merge_preserves_unrelated_data_and_newer_login(self):
        import yaml
        old = normalize_account({"vid": "123", "access_token": "old", "cookie": "wr_vid=123; wr_skey=old"})
        new = {**old, "access_token": "new", "cookie": "wr_vid=123; wr_skey=new"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wx.lic"
            initial = {"token_data": {"token": "other-service"}, "weread_data": {
                "accounts": [old, {"vid": "456", "cookie": "untouched"}], "browser_type": "chrome",
            }}
            path.write_text(yaml.safe_dump(initial))
            persist_account(old, new, lic_path=path)
            saved = yaml.safe_load(path.read_text())
            self.assertEqual(saved["token_data"], initial["token_data"])
            self.assertEqual(saved["weread_data"]["accounts"][1], initial["weread_data"]["accounts"][1])
            self.assertEqual(saved["weread_data"]["accounts"][0]["access_token"], "new")
            persist_account(old, {**old, "access_token": "stale"}, lic_path=path)
            self.assertEqual(yaml.safe_load(path.read_text()), saved)

    def test_signature_matches_reference_vector(self):
        from core.weread_signer import gen_signature
        self.assertEqual(
            gen_signature(["", "410515152", "1780000000000", "123", "test-refresh-token"]),
            "693dacca8f32b01d35a7948afea712da069ebadc21386ee9a95a20b1b5fca59b",
        )

    def test_refreshed_credentials_remain_usable_if_disk_write_fails(self):
        self.session.post.side_effect = [
            response(accessToken="new", skey="new", vid=123), response(),
        ]
        self.session.get.return_value = response()
        self.client.on_refresh = Mock(side_effect=OSError("read-only filesystem"))
        self.assertTrue(self.client.refresh())
        self.assertEqual(self.client.account["access_token"], "new")


if __name__ == "__main__":
    unittest.main()
