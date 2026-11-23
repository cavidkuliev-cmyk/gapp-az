from datetime import datetime, timezone

import feedparser

from gapp import collector

TURBO_FEED = """<?xml version='1.0' encoding='UTF-8'?>
<rss version='2.0' xmlns:turbo='http://turbo.yandex.ru'><channel><title>C</title>
<item turbo='true'><link>https://c.az/post/zasedanie-parlamenta</link>
<description><![CDATA[ Депутаты обсудили бюджет на следующий год ]]></description>
<turbo:content><![CDATA[
<header><h1>В парламенте обсудили &laquo;бюджет&raquo; страны</h1></header>
<p>Текст статьи.</p>]]></turbo:content>
<pubDate>{date}</pubDate></item>
<item turbo='true'><link>https://c.az/post/bez-zagolovka</link>
<description>Запись без заголовка нигде</description><pubDate>{date}</pubDate></item>
</channel></rss>"""


def test_title_is_read_from_turbo_content():
    now = datetime.now(timezone.utc)
    feed = TURBO_FEED.format(date=now.strftime("%a, %d %b %Y %H:%M:%S +0000"))
    entries = feedparser.parse(feed).entries
    item = collector.entry_to_item(entries[0], now)
    assert item["title"] == "В парламенте обсудили «бюджет» страны"
    assert item["url"] == "https://c.az/post/zasedanie-parlamenta"
    assert collector.entry_to_item(entries[1], now) is None
