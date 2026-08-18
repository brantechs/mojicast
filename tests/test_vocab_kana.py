"""単語登録のかな表記ゆれ吸収（ひらがな⇄カタカナ）

認識モデルはかな語をひらがなで出すかカタカナで出すかを選べない
（表記「ぶらんち」で登録しても認識結果は「ブランチ」になる）。
登録した表記へ寄せられること・漢字語の従来挙動が変わらないことを確認する。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vocab import build_replacer, expand_kana, parse_vocab, write_hotwords


class TestExpandKana(unittest.TestCase):
    def test_kana_word_gets_both_forms(self):
        self.assertEqual(expand_kana(["ぶらんち"]), ["ぶらんち", "ブランチ"])
        self.assertEqual(expand_kana(["ブランチ"]), ["ブランチ", "ぶらんち"])

    def test_long_vowel_and_marks_pass_through(self):
        self.assertEqual(expand_kana(["ぶらんちー"]), ["ぶらんちー", "ブランチー"])

    def test_non_kana_is_left_alone(self):
        # 漢字・英数字を含む語は変換しない（誤爆防止）
        for w in ("癒色えも", "VTuber", "Vチューバー", "branch"):
            self.assertEqual(expand_kana([w]), [w])

    def test_no_duplicates_and_order_kept(self):
        self.assertEqual(expand_kana(["ぶらんち", "ブランチ", "えも"]),
                         ["ぶらんち", "ブランチ", "えも", "エモ"])


class TestReplacerKana(unittest.TestCase):
    def test_reading_omitted_kana_surface(self):
        # 読み欄が空でも（parse_vocab は読み=表記にする）カタカナ形が寄る
        r = build_replacer([("ぶらんち", "ぶらんち", None)])
        self.assertEqual(r("今日はブランチの話"), "今日はぶらんちの話")
        self.assertEqual(r("ぶらんちです"), "ぶらんちです")

    def test_katakana_surface_pulls_hiragana(self):
        r = build_replacer([("ブランチ", "ブランチ", None)])
        self.assertEqual(r("ぶらんちの話"), "ブランチの話")

    def test_tolerant_match_still_works(self):
        # SenseVoice等が語中に句読点を挟むケース
        r = build_replacer([("ぶらんち", "ぶらんち", None)])
        self.assertEqual(r("ブ、ランチ"), "ぶらんち")

    def test_kanji_entry_unchanged(self):
        r = build_replacer([("癒色えも", "いろえも", None)])
        self.assertEqual(r("いろえもです"), "癒色えもです")
        self.assertEqual(r("癒色えもです"), "癒色えもです")
        # 読みのカタカナ形も拾えるようになる
        self.assertEqual(r("イロエモだ"), "癒色えもだ")

    def test_protection_pattern_still_wins(self):
        # 本文に正しい表記が出ているとき、短い読みに食われない
        r = build_replacer([("意識エモい系", "いしきえもいけい", None),
                            ("癒色えも", "意識エモ", None)])
        self.assertEqual(r("意識エモい系"), "意識エモい系")


class TestHotwordsKana(unittest.TestCase):
    def test_both_kana_forms_are_boosted(self):
        path = os.path.join(tempfile.mkdtemp(), "hw.txt")
        write_hotwords([("ぶらんち", "ぶらんち", "3.0"),
                        ("癒色えも", "いろえも", None)], path)
        lines = open(path, encoding="utf-8").read().splitlines()
        self.assertEqual(lines, ["ぶらんち :3.0", "ブランチ :3.0",
                                 "いろえも", "イロエモ"])


class TestParseVocab(unittest.TestCase):
    def test_reading_defaults_to_surface(self):
        path = os.path.join(tempfile.mkdtemp(), "v.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# コメント\nぶらんち\n癒色えも,いろえも,3.0\n")
        self.assertEqual(parse_vocab(path),
                         [("ぶらんち", "ぶらんち", None),
                          ("癒色えも", "いろえも", "3.0")])


if __name__ == "__main__":
    unittest.main()
