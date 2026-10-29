"""Free, keyless market data; Telegram credentials only in environment."""
import argparse
import csv
import io
import json
import logging
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
LOG = logging.getLogger(__name__)


class FeedError(Exception):
    pass


def fetch(url, attempts=3):
    # Do not log exception text: Telegram URLs contain the bot token.
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 MarketDigest/1.0"})
            with urllib.request.urlopen(req, timeout=15) as response:
                return response.read()
        except (OSError, urllib.error.URLError):
            if attempt + 1 < attempts:
                time.sleep(2 ** attempt)
    raise FeedError("行情接口请求失败") from None


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("invalid price")
    return number


@dataclass
class Quote:
    value: float
    label: str
    source: str
    updated: str
    note: str


def parse_yahoo(raw, now):
    data = json.loads(raw)["chart"]
    if data.get("error"):
        raise ValueError("Yahoo error")
    meta = data["result"][0]["meta"]
    if meta["symbol"] != "BZ=F" or meta["currency"] != "USD":
        raise ValueError("wrong instrument")
    stamp = datetime.fromtimestamp(int(meta["regularMarketTime"]), MSK)
    if stamp > now + timedelta(minutes=10) or now - stamp > timedelta(days=7):
        raise ValueError("invalid/stale timestamp")
    note = "延时报价；休市时保留最近成交价"
    if now - stamp > timedelta(hours=6):
        note += "；⚠️ 行情距今已超过6小时"
    return Quote(positive(meta["regularMarketPrice"]), "布伦特原油期货（BZ=F）",
                 "Yahoo Finance / NYMEX", stamp.strftime("%Y-%m-%d %H:%M:%S MSK"), note)


def parse_fred(raw, now):
    rows = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    valid = []
    for row in rows:
        day = row.get("observation_date") or row.get("DATE")
        if not day:
            continue
        try:
            observed = datetime.strptime(day, "%Y-%m-%d").date()
            price = positive(row["DCOILBRENTEU"])
        except (ValueError, TypeError, KeyError):
            continue
        if observed <= now.date():
            valid.append((observed, price))
    if not valid:
        raise ValueError("no Brent observations")
    day, price = max(valid)
    if (now.date() - day).days > 14:
        raise ValueError("FRED data older than 14 days")
    return Quote(price, "布伦特原油现货（日度备用）", "美国 EIA / FRED · DCOILBRENTEU",
                 f"{day}（数据所属日；接口未提供发布时间）",
                 "⚠️ 期货源不可用，已切换现货；日度数据通常滞后数日，与期货口径不同")


class EIATable(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.row = []
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        if tag in ("td", "th"):
            self.cell = []

    def handle_data(self, text):
        if self.cell is not None:
            self.cell.append(text)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        if tag == "tr":
            self.rows.append(self.row)
            self.row = []


def parse_eia(raw, now):
    parser = EIATable()
    parser.feed(raw.decode("utf-8"))
    observations = []
    for row in parser.rows:
        if len(row) != 6:
            continue
        match = re.fullmatch(r"(\d{4})\s+([A-Z][a-z]{2})-\s*(\d{1,2})\s+to\s+.*", row[0])
        if not match:
            continue
        monday = datetime.strptime(" ".join(match.groups()), "%Y %b %d").date()
        if monday.weekday() != 0:
            continue
        for offset, value in enumerate(row[1:]):
            day = monday + timedelta(days=offset)
            if day <= now.date():
                try:
                    observations.append((day, positive(value)))
                except (ValueError, TypeError):
                    pass
    if not observations:
        raise ValueError("no EIA daily observations")
    day, price = max(observations)
    if (now.date() - day).days > 14:
        raise ValueError("EIA data older than 14 days")
    return Quote(price, "布伦特原油现货（日度备用）", "美国能源信息署 EIA · Europe Brent Spot FOB",
                 f"{day}（数据所属日；无日内更新时间）",
                 "⚠️ 期货源不可用，已切换现货；官方历史日度表可能滞后数日，与期货口径不同")


def brent(now):
    for host in ("query1", "query2"):
        try:
            return parse_yahoo(fetch(f"https://{host}.finance.yahoo.com/v8/finance/chart/BZ%3DF?interval=1d&range=5d"), now)
        except (FeedError, ValueError, KeyError, TypeError, IndexError, OverflowError, OSError):
            LOG.warning("Yahoo %s unavailable", host)
    try:
        return parse_eia(fetch("https://www.eia.gov/dnav/pet/hist/RBRTED.htm"), now)
    except (FeedError, ValueError, KeyError, TypeError, IndexError):
        LOG.warning("EIA unavailable; trying FRED")
    start = (now - timedelta(days=60)).date()
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id=DCOILBRENTEU&cosd={start}"
    return parse_fred(fetch(url), now)


def parse_cbr(raw, now):
    root = ET.fromstring(raw)
    day = datetime.strptime(root.attrib["Date"], "%d.%m.%Y").date()
    if day > now.date() or (now.date() - day).days > 14:
        raise ValueError("invalid CBR effective date")
    for row in root.findall("Valute"):
        if row.findtext("CharCode") == "CNY":
            nominal = Decimal(row.findtext("Nominal"))
            if nominal <= 0:
                raise ValueError("invalid nominal")
            value = Decimal(row.findtext("Value").replace(",", ".")) / nominal
            return Quote(positive(value), "人民币兑卢布", "俄罗斯央行 CBR",
                         f"{day}（汇率生效日；接口未提供发布时间）",
                         "官方日度参考汇率；非银行买卖价，日内通常不变" +
                         ("；⚠️ 生效日距今超过4天" if (now.date()-day).days > 4 else ""))
    raise ValueError("CNY missing")


def cny_rub(now):
    # Explicit date prevents tomorrow's published fixing appearing as today's rate.
    date = now.strftime("%d/%m/%Y")
    query = urllib.parse.urlencode({"date_req": date})
    return parse_cbr(fetch("https://www.cbr.ru/scripts/XML_daily.asp?" + query), now)


def parse_gold(raw, now):
    data = json.loads(raw)
    if data["symbol"] != "XAU" or data.get("currency") != "CNY":
        raise ValueError("wrong gold instrument/currency")
    stamp = datetime.fromisoformat(data["updatedAt"].replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("gold timestamp has no timezone")
    stamp = stamp.astimezone(MSK)
    if stamp > now + timedelta(minutes=10) or now - stamp > timedelta(days=4):
        raise ValueError("invalid/stale gold timestamp")
    note = "⚠️ 行情较旧" if now - stamp > timedelta(minutes=15) else ""
    # Gold API quotes per troy ounce; convert to CNY per gram.
    return Quote(positive(data["price"]) / 31.1034768, "国际现货黄金", "Gold API",
                 stamp.strftime("%Y-%m-%d %H:%M:%S MSK"), note)


def gold(now):
    return parse_gold(fetch("https://api.gold-api.com/price/XAU/CNY"), now)


def build_message(now):
    sections = ["📊 每日行情", f"{now:%Y-%m-%d %H:%M} MSK", ""]
    failed = False
    for getter in (brent, cny_rub, gold):
        try:
            quote = getter(now)
            if getter is brent:
                price, label = f"{quote.value:.2f} USD/桶", "布伦特期货"
            elif getter is cny_rub:
                price, label = f"1 CNY = {quote.value:.4f} RUB", "人民币兑卢布"
            else:
                price, label = f"{quote.value:.2f} 元/克", "国际现货黄金"
            if getter is brent and "现货" in quote.label:
                label = "布伦特现货（日度备用）"
            sections += [label, price]
            if "⚠️" in quote.note:
                sections.append("⚠️ 已切换日度现货参考价" if "现货" in quote.label else "⚠️ 行情较旧")
            sections.append("")
        except (FeedError, ValueError, KeyError, TypeError, IndexError, OverflowError, ET.ParseError, OSError):
            failed = True
            label = ("布伦特原油" if getter is brent else
                     "人民币兑卢布" if getter is cny_rub else "国际现货黄金")
            sections += [f"⚠️ {label}：本次获取失败，未使用缓存价格。", ""]
            LOG.error("%s feed failed", label)
    return "\n".join(sections).strip(), failed


def telegram(token, method, payload):
    if not token or any(c.isspace() for c in token) or "/" in token or ":" not in token:
        raise FeedError("Telegram Token 格式无效")
    url = f"https://api.telegram.org/bot{token}/{method}"
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    # Deliberately no retry on ambiguous send failures: Telegram has no idempotency key.
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            result = json.load(response)
    except (OSError, ValueError):
        raise FeedError("Telegram 请求失败；检查网络、Token、Chat ID 和机器人权限。未自动重发以避免重复消息") from None
    if not result.get("ok"):
        raise FeedError("Telegram 返回失败，请检查配置")
    return result["result"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Fetch live prices without sending")
    args = parser.parse_args()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not args.dry_run and (not token or not chat):
        LOG.error("请配置 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID Secrets")
        return 1
    message, failed = build_message(datetime.now(MSK))
    if args.dry_run:
        print(message)
    else:
        try:
            telegram(token, "sendMessage", {"chat_id": chat, "text": message})
            LOG.info("Telegram message accepted")
        except FeedError as error:
            LOG.error("%s", error)
            return 1
    # Alert delivered, but make data failure visible in GitHub Actions too.
    return int(failed)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    sys.exit(main())
