"""M2M-100訳の反復暴走・空出力の抑止（translate.translate_m2m）

greedy(beam_size=1)は感動詞・繰り返し口語で反復暴走し、まれに <unk> だけを
出して空になる。反復抑止のパラメータが実際に渡ること・空のときだけ
beam_size=2 で引き直すことを、モデル無し（フェイク差し替え）で確認する。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import translate


class FakeResult:
    """CTranslate2 の TranslationResult 相当（hypotheses だけ持つ）"""

    def __init__(self, tokens):
        self.hypotheses = [tokens]


class FakeM2M:
    """translate_batch の呼び出しを記録し、用意した結果を順に返す"""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def translate_batch(self, source, **kw):
        self.calls.append((source, kw))
        tokens = self.outputs.pop(0) if self.outputs else []
        return [FakeResult(tokens)]


class FakeSP:
    """SentencePiece 相当（1文字=1トークンの単純な変換）"""

    def encode(self, text, out_type=str):
        return list(text)

    def decode(self, tokens):
        return "".join(tokens)


class TranslateM2MBase(unittest.TestCase):
    def use_fakes(self, *outputs):
        m2m = FakeM2M(outputs)
        for name, obj in (("_m2m", m2m), ("_sp_m2m", FakeSP())):
            orig = getattr(translate, name)
            setattr(translate, name, obj)
            self.addCleanup(setattr, translate, name, orig)
        return m2m


class TestRepetitionParams(TranslateM2MBase):
    def test_repetition_params_are_passed(self):
        m2m = self.use_fakes(["__zh__", "你", "好", "</s>"])
        self.assertEqual(translate.translate_m2m("こんにちは"), "你好")
        _, kw = m2m.calls[0]
        self.assertEqual(kw["repetition_penalty"], 1.2)
        self.assertEqual(kw["no_repeat_ngram_size"], 3)
        self.assertEqual(kw["beam_size"], 1)

    def test_default_is_same_for_every_language(self):
        # 韓国語だけの特例をやめ、全言語で 1.2 を既定にした
        for tgt in ("zh", "ko", "en", "id"):
            with self.subTest(tgt=tgt):
                m2m = self.use_fakes(["__%s__" % tgt, "x", "</s>"])
                translate.translate_m2m("テスト", "ja", tgt)
                self.assertEqual(m2m.calls[0][1]["repetition_penalty"], 1.2)

    def test_explicit_penalty_is_kept(self):
        m2m = self.use_fakes(["__zh__", "好", "</s>"])
        translate.translate_m2m("テスト", "ja", "zh", repetition_penalty=1.05)
        self.assertEqual(m2m.calls[0][1]["repetition_penalty"], 1.05)


class TestEmptyRetry(TranslateM2MBase):
    def test_retry_with_beam2_when_empty(self):
        # 1回目が <unk> だけ（=空）なら beam_size=2 で引き直す
        m2m = self.use_fakes(["__zh__", "<unk>", "</s>"],
                             ["__zh__", "乌", "冬", "面", "</s>"])
        self.assertEqual(translate.translate_m2m("うどん"), "乌冬面")
        self.assertEqual(len(m2m.calls), 2)
        self.assertEqual(m2m.calls[0][1]["beam_size"], 1)
        self.assertEqual(m2m.calls[1][1]["beam_size"], 2)
        self.assertEqual(m2m.calls[1][1]["no_repeat_ngram_size"], 3)

    def test_empty_stays_empty_after_retry(self):
        m2m = self.use_fakes(["__zh__", "<unk>", "</s>"], ["__zh__", "</s>"])
        self.assertEqual(translate.translate_m2m("うどん"), "")
        self.assertEqual(len(m2m.calls), 2)

    def test_no_retry_when_not_empty(self):
        m2m = self.use_fakes(["__zh__", "你", "好", "</s>"])
        self.assertEqual(translate.translate_m2m("こんにちは"), "你好")
        self.assertEqual(len(m2m.calls), 1)

    def test_blank_input_does_not_call_model(self):
        m2m = self.use_fakes(["__zh__", "你", "好", "</s>"])
        self.assertEqual(translate.translate_m2m("   "), "")
        self.assertEqual(m2m.calls, [])


if __name__ == "__main__":
    unittest.main()
