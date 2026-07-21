from __future__ import annotations

import unittest
from unittest import mock

import requests

from feishu_doc import FeishuDocError, _request_with_retry


class FeishuDocRetryTests(unittest.TestCase):
    def test_request_with_retry_recovers_after_timeouts(self) -> None:
        response = mock.Mock()
        side_effects = [requests.ReadTimeout("first timeout"), requests.ReadTimeout("second timeout"), response]
        with mock.patch("feishu_doc.requests.request", side_effect=side_effects) as mocked_request:
            with mock.patch("feishu_doc.time.sleep") as mocked_sleep:
                result = _request_with_retry(
                    method="POST",
                    url="https://open.feishu.cn/open-apis/test",
                    timeout=60,
                    request_retries=3,
                    retry_backoff_seconds=0.1,
                    safe_to_retry=True,
                    headers={"Authorization": "Bearer t"},
                )
        self.assertIs(result, response)
        self.assertEqual(mocked_request.call_count, 3)
        self.assertEqual(mocked_sleep.call_count, 2)

    def test_request_with_retry_raises_after_exhausted(self) -> None:
        with mock.patch("feishu_doc.requests.request", side_effect=requests.ReadTimeout("still timeout")) as mocked_request:
            with mock.patch("feishu_doc.time.sleep"):
                with self.assertRaises(FeishuDocError):
                    _request_with_retry(
                        method="GET",
                        url="https://open.feishu.cn/open-apis/test",
                        timeout=60,
                        request_retries=2,
                        retry_backoff_seconds=0.0,
                    )
        self.assertEqual(mocked_request.call_count, 2)

    def test_request_with_retry_recovers_from_temporary_503(self) -> None:
        unavailable = mock.Mock(status_code=503)
        success = mock.Mock(status_code=200)
        with mock.patch("feishu_doc.requests.request", side_effect=[unavailable, success]) as mocked_request:
            with mock.patch("feishu_doc.time.sleep"):
                result = _request_with_retry(
                    method="POST",
                    url="https://open.feishu.cn/open-apis/test",
                    timeout=60,
                    request_retries=2,
                    retry_backoff_seconds=0.1,
                    safe_to_retry=True,
                )
        self.assertIs(result, success)
        self.assertEqual(mocked_request.call_count, 2)
        unavailable.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
