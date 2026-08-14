import unittest

import app_server


class TranslationSlotTests(unittest.TestCase):
    """翻訳SSEに付ける slot（翻訳先の並び順）。overlay の part=trN 振り分け用。"""

    def slot(self, lang, **cfg):
        return app_server.translation_slot(cfg, lang)

    def test_first_target(self):
        self.assertEqual(self.slot("en", translate_lang="en"), 1)

    def test_second_and_third_target(self):
        cfg = {"translate_lang": "en", "translate_lang2": "zh",
               "translate_lang3": "ko"}
        self.assertEqual(app_server.translation_slot(cfg, "zh"), 2)
        self.assertEqual(app_server.translation_slot(cfg, "ko"), 3)

    def test_unset_targets_are_skipped(self):
        # 2つ目が空でも3つ目は3のまま（overlayのtr3と対応させる）
        cfg = {"translate_lang": "en", "translate_lang2": "",
               "translate_lang3": "zh_tw"}
        self.assertEqual(app_server.translation_slot(cfg, "zh_tw"), 3)
        self.assertEqual(app_server.translation_slot(cfg, ""), 0)

    def test_unknown_lang(self):
        self.assertEqual(self.slot("id", translate_lang="en"), 0)
        self.assertEqual(app_server.translation_slot({}, "en"), 0)


if __name__ == "__main__":
    unittest.main()
