from __future__ import annotations

import json
import unittest

from extra_auth import _collect_auth_data


class MemoryHar:
    def __init__(self, entries):
        self.entries = entries

    def read_text(self, *, encoding: str) -> str:
        assert encoding == "utf-8"
        return json.dumps({"log": {"entries": self.entries}})


def har_entry(url: str, status: int, *, cookie: str = "") -> dict:
    headers = [{"name": "Cookie", "value": cookie}] if cookie else []
    return {
        "request": {"url": url, "headers": headers, "cookies": []},
        "response": {"status": status, "headers": []},
    }


class HarSelectionTests(unittest.TestCase):
    def test_uses_last_successful_target_request_only(self) -> None:
        entries = [
            har_entry(
                "https://fenxi.4399dev.com/event-analysis-server/query?access_token=qz4399doc-OLD",
                200,
                cookie="e_token=OLD",
            ),
            har_entry(
                "https://unrelated.example/api?access_token=qz4399doc-WRONG-DOMAIN",
                200,
                cookie="e_token=WRONG-DOMAIN",
            ),
            har_entry(
                "https://fenxi.4399dev.com/event-analysis-server/query?access_token=qz4399doc-WRONG-STATUS",
                500,
                cookie="e_token=WRONG-STATUS",
            ),
            har_entry(
                "https://fenxi.4399dev.com/event-analysis-server/query?access_token=qz4399doc-LATEST",
                204,
                cookie="e_token=LATEST",
            ),
        ]
        auth = _collect_auth_data([MemoryHar(entries)], "fenxi")
        self.assertEqual(auth["token"], "qz4399doc-LATEST")
        self.assertEqual(auth["cookies"]["e_token"], "LATEST")
        self.assertNotIn("WRONG-DOMAIN", auth["cookies"].values())
        self.assertNotIn("WRONG-STATUS", auth["cookies"].values())


if __name__ == "__main__":
    unittest.main()
