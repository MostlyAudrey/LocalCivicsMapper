"""Parallel crawling: different sites run concurrently, same-host sources never overlap."""
import asyncio

from civic_tracker.crawler.scraper import for_each_source, host_groups, source_hosts


def _src(i, base, **cfg):
    return {"id": i, "name": f"s{i}", "base_url": base, "rss_url": None, "sitemap_url": None, "scrape_config": cfg}


def test_source_hosts_and_groups():
    a = _src(1, "https://www.example.org")
    b = _src(2, "https://example.org/yolo")                       # same host as a (www. ignored)
    c = _src(3, "https://davisite.org", wp_api_base="https://public-api.wordpress.com/wp/v2/sites/davisite.org")
    d = _src(4, "https://other.org", wp_api_base="https://public-api.wordpress.com/wp/v2/sites/other.org")
    e = _src(5, "https://lonely.net")
    assert source_hosts(c) == {"davisite.org", "public-api.wordpress.com"}
    groups = [[s["id"] for s in g] for g in host_groups([a, c, e, b, d])]
    assert groups == [[1, 2], [3, 4], [5]]  # Davisite + other share the WordPress.com API host


def test_bridging_source_merges_groups():
    a = _src(1, "https://a.org")
    b = _src(2, "https://b.org")
    bridge = _src(3, "https://a.org", listing_urls=["https://b.org/news"])
    groups = host_groups([a, b, bridge])
    assert len(groups) == 1 and sorted(s["id"] for s in groups[0]) == [1, 2, 3]


def test_for_each_source_concurrency():
    sources = [_src(1, "https://a.org"), _src(2, "https://a.org/x"), _src(3, "https://b.org"),
               _src(4, "https://c.org"), _src(5, "https://d.org")]
    active_hosts, peak, overlap = [], [0], []

    async def fn(s):
        host = s["base_url"].split("/")[2]
        if host in active_hosts:
            overlap.append(host)
        active_hosts.append(host)
        peak[0] = max(peak[0], len(active_hosts))
        await asyncio.sleep(0.05)
        active_hosts.remove(host)

    asyncio.run(for_each_source(sources, fn, parallel=3))
    assert not overlap          # a.org's two sources never ran together
    assert peak[0] == 3         # capped at `parallel`, but more than one at a time


def test_polite_gaps_are_random_never_below_delay():
    from civic_tracker.crawler.polite import PoliteClient

    c = PoliteClient(delay=5, jitter_factor=2.0, pause_chance=0.05, pause_range=(120, 600))
    gaps = [c.next_gap() for _ in range(4000)]
    asyncio.run(c.aclose())
    short = [g for g in gaps if g < 100]
    assert min(gaps) >= 5 and max(short) <= 15          # 5-15 s between ordinary requests
    assert len(set(round(g, 2) for g in short)) > 500   # actually varies
    assert 100 < c.pauses_taken < 300                   # ~5% long breaks
    assert all(125 <= g <= 615 for g in gaps if g >= 100)
    assert PoliteClient(delay=0).next_gap() == 0        # test clients stay unthrottled


def test_crawl_group_serializes_same_platform_sources():
    a = _src(1, "https://www.davisenterprise.com", crawl_group="townnews-blox")
    b = _src(2, "https://www.wintersexpress.com", crawl_group="townnews-blox")
    c = _src(3, "https://other.org")
    assert sorted(sorted(s["id"] for s in g) for g in host_groups([a, b, c])) == [[1, 2], [3]]
