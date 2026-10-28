import json
import unittest
from datetime import datetime
from unittest.mock import patch

import bot

NOW = datetime(2026, 10, 29, 12, tzinfo=bot.MSK)


class MarketTests(unittest.TestCase):
    def test_cbr_nominal_and_effective_date(self):
        raw = b'<ValCurs Date="29.10.2026"><Valute><CharCode>CNY</CharCode><Nominal>10</Nominal><Value>115,4321</Value></Valute></ValCurs>'
        quote = bot.parse_cbr(raw, NOW)
        self.assertAlmostEqual(quote.value, 11.54321)
        self.assertIn("2026-10-29", quote.updated)
        self.assertIn("非银行买卖价", quote.note)

    def test_cbr_rejects_future(self):
        with self.assertRaises(ValueError):
            bot.parse_cbr(b'<ValCurs Date="30.10.2026"/>', NOW)

    def test_yahoo_time_and_staleness(self):
        stamp = datetime(2026, 10, 28, 21, tzinfo=bot.MSK)
        meta = dict(symbol="BZ=F", currency="USD", regularMarketPrice=80.5,
                    regularMarketTime=int(stamp.timestamp()))
        quote = bot.parse_yahoo(json.dumps({"chart": {"result": [{"meta": meta}]}}), NOW)
        self.assertEqual(quote.value, 80.5)
        self.assertIn("21:00:00 MSK", quote.updated)
        self.assertIn("超过6小时", quote.note)
        meta["regularMarketTime"] = 0
        with self.assertRaises(ValueError):
            bot.parse_yahoo(json.dumps({"chart": {"result": [{"meta": meta}]}}), NOW)

    def test_fred_skips_missing_and_future_and_labels_spot(self):
        quote = bot.parse_fred(b'observation_date,DCOILBRENTEU\n2026-10-28,.\n2026-10-27,78.33\n2026-10-30,99\n', NOW)
        self.assertEqual(quote.value, 78.33)
        self.assertIn("现货", quote.label)
        self.assertIn("2026-10-27", quote.updated)

    def test_invalid_prices(self):
        for value in ("nan", "inf", -1, 0):
            with self.assertRaises(ValueError):
                bot.positive(value)

    def test_feed_failure_still_reports_other_quote(self):
        q = bot.Quote(11.5, "人民币兑卢布", "CBR", "2026-10-29", "日度")
        with patch.object(bot, "brent", side_effect=bot.FeedError), patch.object(bot, "cny_rub", return_value=q):
            text, failed = bot.build_message(NOW)
        self.assertTrue(failed)
        self.assertIn("本次获取失败", text)
        self.assertIn("1 CNY = 11.5000 RUB", text)

    def test_yahoo_failure_uses_fred(self):
        with patch.object(bot, "fetch", side_effect=[bot.FeedError(), bot.FeedError(), bot.FeedError(),
                         b'observation_date,DCOILBRENTEU\n2026-10-28,80\n']):
            self.assertIn("现货", bot.brent(NOW).label)

    def test_eia_preserves_empty_weekday_cells(self):
        data = b'<table><tr><td>2026 Oct-26 to Oct-30</td><td></td><td>123.10</td><td></td><td></td><td></td></tr></table>'
        quote = bot.parse_eia(data, NOW)
        self.assertEqual(quote.value, 123.10)
        self.assertIn("2026-10-27", quote.updated)
        self.assertIn("EIA", quote.source)

    def test_telegram_timeout_does_not_retry_or_leak_token(self):
        with patch("urllib.request.urlopen", side_effect=TimeoutError("secret-url")) as call:
            with self.assertRaises(bot.FeedError) as caught:
                bot.telegram("123:private", "sendMessage", {"chat_id": "1", "text": "test"})
        self.assertEqual(call.call_count, 1)
        self.assertNotIn("private", str(caught.exception))
        self.assertNotIn("secret-url", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
