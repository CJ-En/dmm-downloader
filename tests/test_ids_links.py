import unittest

from dmm_downloader.ids import output_name, parse_cid, parse_lines
from dmm_downloader.links import NoLinks, choose, extract_json_links, extract_page_links, group_links

PROXY = "https://www.dmm.co.jp/monthly/premium/-/proxy/=/product_id={pid}/transfer_type=download/rate={rate}{part}/drm=1/ftype=dcv"


def link(rate, part=None, pid="abc00123", codec=None):
    url = PROXY.format(pid=pid, rate=rate, part=f"/part={part}" if part else "")
    return url.replace("/drm=1", f"/codec={codec}/drm=1") if codec else url


class IdsTest(unittest.TestCase):
    def test_parse_cid(self):
        self.assertEqual(parse_cid("ABC00123"), "abc00123")
        self.assertEqual(parse_cid(" h_1133gone00123 "), "h_1133gone00123")
        self.assertEqual(parse_cid("https://www.dmm.co.jp/digital/videoa/-/detail/=/cid=ssis00123/?i3_ref=x"), "ssis00123")
        self.assertEqual(parse_cid("https://video.dmm.co.jp/av/content/?id=abc00123&cid=1abc00010"), "1abc00010")
        self.assertEqual(parse_cid("https://video.dmm.co.jp/av/content/?id=SONE00876"), "sone00876")
        self.assertEqual(parse_cid("https://video.dmm.co.jp/amateur/content/?i3_ref=x&id=h_1133gone00123#top"), "h_1133gone00123")
        for bad in ("ABC-123", "../etc", "abc 123", "a\\b", "", "x" * 81, "https://www.dmm.co.jp/x/?product_id=abc00123",
                    "https://video.dmm.co.jp/av/content/?id=ABC-123"):
            self.assertIsNone(parse_cid(bad), bad)

    def test_padding_is_kept(self):
        self.assertEqual(parse_lines(["sone096", "sone00096", "sone096"])[0], ["sone096", "sone00096"])

    def test_parse_lines(self):
        found, invalid = parse_lines([
            "abc00123, xyz00456  # a comment abc99999",
            "# whole comment line",
            "https://www.dmm.co.jp/digital/videoa/-/detail/=/cid=def00789/#review",
            "SSIS-123",
            "",
        ])
        self.assertEqual(found, ["abc00123", "xyz00456", "def00789"])
        self.assertEqual(invalid, ["SSIS-123"])

    def test_output_name(self):
        self.assertEqual(output_name("1abc00010"), "1abc00010.mkv")
        self.assertEqual(output_name("1abc00010", 2), "1abc00010-pt2.mkv")
        with self.assertRaises(ValueError):
            output_name("../x")


class LinksTest(unittest.TestCase):
    def test_page_links_and_best_choice(self):
        html = "".join(
            f'<a href="{u}">dl</a>'
            for u in [link("3000"), link("6000", 2), link("6000", 1), link("4k", 1), link("4k", 2), link("4k", 3)]
        )
        links = extract_page_links(html)
        self.assertEqual(set(links), {"3000", "6000", "4k"})
        label, urls = choose(links)
        self.assertEqual(label, "4k")
        self.assertEqual([u.split("part=")[1][0] for u in urls], ["1", "2", "3"])
        label, urls = choose(links, "6000")
        self.assertEqual((label, len(urls)), ("6000", 2))
        with self.assertRaises(NoLinks):
            choose(links, "9999")

    def test_parts_in_numeric_order(self):
        urls = [link("4k", n) for n in (10, 2, 1, 3, 4, 5, 6, 7, 8, 9)]
        ordered = group_links({"4k": urls})["4k"]
        self.assertEqual([int(u.split("part=")[1].split("/")[0]) for u in ordered], list(range(1, 11)))

    def test_vp9_before_h264_at_same_rate(self):
        links = {"4k": [link("4k", 1, codec="h264"), link("4k", 2, codec="h264"), link("4k", 1, codec="vp9"), link("4k", 2, codec="vp9")]}
        label, urls = choose(links)
        self.assertEqual(label, "4k/vp9")
        self.assertEqual(len(urls), 2)

    def test_script_fallback(self):
        html = """<script>var items = {0: {product_id: "abc00123dl", rate: "4k", volume: "2"},
        1: {product_id: "abc00123dl", rate: "3000"}};</script>"""
        links = extract_page_links(html)
        self.assertEqual(len(links["4k"]), 2)
        self.assertIn("/part=2/", links["4k"][1])
        self.assertEqual(len(links["3000"]), 1)
        self.assertNotIn("part=", links["3000"][0])

    def test_json_links_only_from_dmm(self):
        payload = {"data": [{"url": "/digital/-/proxy/=/rate=4k/part=1/ftype=dcv"},
                            {"x": {"y": "https://evil.example/rate=4k/ftype=dcv"}}]}
        links = extract_json_links(payload)
        self.assertEqual(list(links), ["4k"])
        self.assertTrue(links["4k"][0].startswith("https://www.dmm.co.jp/"))

    def test_nothing_found(self):
        self.assertEqual(extract_page_links("<html><a href='/x'>no</a></html>"), {})
        with self.assertRaises(NoLinks):
            choose({})


if __name__ == "__main__":
    unittest.main()
