"""開発者ログ（logs/日付/*.jsonl）— 開閉の条件と、翻訳・確定文の記録内容"""
import glob
import json
import os
import queue
import shutil
import tempfile
import unittest
from datetime import datetime

import engine


class FrozenDatetime(datetime):
    """now() だけ固定する差し替え用（fromtimestamp 等は本物のまま）"""

    _now = datetime(2026, 8, 16, 21, 30, 0)

    @classmethod
    def now(cls, tz=None):
        return cls._now


class DevLogBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mojicast_devlog_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        orig_base = engine.DATA_BASE
        engine.DATA_BASE = self.tmp
        self.addCleanup(setattr, engine, "DATA_BASE", orig_base)
        orig_dt = engine.datetime
        engine.datetime = FrozenDatetime
        self.addCleanup(setattr, engine, "datetime", orig_dt)
        self.eng = engine.CaptionEngine()
        self.addCleanup(self.eng._close_log)

    # --- ヘルパ ---

    def open_session(self, **cfg):
        cfg.setdefault("save_log", False)   # 既定は開発者ログだけを見る
        self.eng._open_log(cfg)

    def dev_path(self):
        found = glob.glob(os.path.join(self.tmp, "logs", "*", "*.jsonl"))
        self.assertEqual(len(found), 1, f"jsonlが1つでない: {found}")
        return found[0]

    def events(self):
        with open(self.dev_path(), encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]


class DevLogSessionTests(DevLogBase):
    """開発者モードのON/OFFで、ファイルが増えるかどうか"""

    def test_off_writes_nothing(self):
        self.open_session(dev_log=False)
        self.eng._dev_event("final", text="こんばんは")
        self.assertEqual(glob.glob(os.path.join(self.tmp, "logs", "*", "*")), [])

    def test_on_writes_session_header(self):
        self.open_session(dev_log=True, asr_model="k2-ja", translate=True,
                          translate_lang="en", translate_lang2="zh")
        evs = self.events()
        self.assertEqual(evs[0]["ev"], "session")
        self.assertEqual(evs[0]["asr_model"], "k2-ja")
        self.assertEqual(evs[0]["langs"], ["en", "zh", ""])
        self.assertTrue(evs[0]["ts"].startswith("2026-08-16 21:30:00."))

    def test_independent_of_caption_log(self):
        """字幕ログOFFでも開発者ログは残る（逆に字幕ログは作られない）"""
        self.open_session(dev_log=True, save_log=False)
        self.eng._log_final("こんばんは")
        self.assertTrue(os.path.exists(self.dev_path()))
        self.assertEqual(glob.glob(os.path.join(self.tmp, "logs", "*", "*.log")), [])

    def test_close_then_write_is_noop(self):
        """停止後に遅れて届いた翻訳が、閉じたファイルへ書こうとしても落ちない"""
        self.open_session(dev_log=True)
        self.eng._close_log()
        self.eng._dev_event("translate", out="hello")
        self.assertEqual([e["ev"] for e in self.events()], ["session"])

    def test_reopen_without_dev_log_stops_logging(self):
        """開発者モードをOFFにして開き直すと、次のセッションからは書かれない"""
        self.open_session(dev_log=True)
        first = self.dev_path()
        FrozenDatetime._now = datetime(2026, 8, 16, 22, 0, 0)
        self.open_session(dev_log=False)
        self.eng._dev_event("final", text="こんばんは")
        self.assertEqual(
            glob.glob(os.path.join(self.tmp, "logs", "*", "*.jsonl")), [first])


class DevLogFinalTests(DevLogBase):
    """確定文の後処理を、変わった段階だけ記録する"""

    def steps_of(self, ev):
        return [(s["by"], s["text"]) for s in ev["steps"]]

    def test_records_only_changed_stages(self):
        steps = [("asr", "三十五点")]
        engine._note_step(steps, "replace", "三十五点")   # 変化なし → 残らない
        engine._note_step(steps, "num", "35点")
        self.open_session(dev_log=True)
        self.eng._dev_final(7, "自分", "35点。", steps + [("punct", "35点。")])
        ev = self.events()[-1]
        self.assertEqual(ev["ev"], "final")
        self.assertEqual(ev["fid"], 7)
        self.assertEqual(ev["speaker"], "自分")
        self.assertEqual(ev["text"], "35点。")
        self.assertEqual(self.steps_of(ev),
                         [("asr", "三十五点"), ("num", "35点"), ("punct", "35点。")])

    def test_dropped_line_is_recorded(self):
        """後処理で消えた行は drop として残す（画面にも字幕ログにも出ないため）"""
        self.open_session(dev_log=True)
        self.eng._dev_final(None, "", "", [("asr", "えー"), ("mask", "")])
        ev = self.events()[-1]
        self.assertEqual(ev["ev"], "drop")
        self.assertIsNone(ev["fid"])

    def test_empty_recognition_is_not_recorded(self):
        self.open_session(dev_log=True)
        self.eng._dev_final(None, "", "", [("asr", "")])
        self.eng._dev_final(3, "", "こんばんは", None)   # 開発者モードOFF相当
        self.assertEqual([e["ev"] for e in self.events()], ["session"])


class DevLogTranslateTests(DevLogBase):
    """翻訳ワーカーが、入力・出力・所要時間を翻訳先ごとに残す"""

    def run_worker(self, *items):
        self.eng._tq = queue.Queue()
        for item in items:
            self.eng._tq.put(item)
        self.eng._tq.put(None)          # 停止サイン
        self.eng._translate_loop()

    def test_records_each_target(self):
        self.open_session(dev_log=True)
        self.eng._translate_on = True
        self.eng._translators = [("fugumt", "en", lambda t: "good evening"),
                                 ("m2m", "zh", lambda t: "晚上好")]
        self.run_worker((5, "こんばんは"))
        evs = [e for e in self.events() if e["ev"] == "translate"]
        self.assertEqual([(e["fid"], e["engine"], e["lang"], e["out"]) for e in evs],
                         [(5, "fugumt", "en", "good evening"),
                          (5, "m2m", "zh", "晚上好")])
        self.assertEqual(evs[0]["src"], "こんばんは")
        self.assertGreaterEqual(evs[0]["ms"], 0)
        self.assertNotIn("glossed", evs[0])   # 辞書置換なし
        self.assertNotIn("error", evs[0])

    def test_records_glossary_substitution(self):
        """英訳辞書で置換した「実際に翻訳へ渡した文」も残す（日→英のみ）"""
        self.open_session(dev_log=True)
        self.eng._translate_on = True
        self.eng._gloss = [("文字キャスト", "Mojicast")]
        self.eng._translators = [("fugumt", "en", lambda t: t.upper())]
        self.run_worker((1, "文字キャストを起動"))
        ev = self.events()[-1]
        self.assertEqual(ev["src"], "文字キャストを起動")
        self.assertEqual(ev["glossed"], "Mojicastを起動")

    def test_records_failure(self):
        self.open_session(dev_log=True)
        self.eng._translate_on = True

        def boom(_t):
            raise RuntimeError("模擬失敗")

        self.eng._translators = [("m2m", "ko", boom)]
        self.run_worker((2, "こんばんは"))
        ev = self.events()[-1]
        self.assertEqual(ev["out"], "")
        self.assertIn("模擬失敗", ev["error"])
        self.assertFalse(ev["shown"])
        # 既存の translate_error.log にも従来どおり残る
        self.assertTrue(os.path.exists(
            os.path.join(self.tmp, "translate_error.log")))

    def test_marks_result_not_shown(self):
        """停止直後などで画面に出なかった訳も、出なかったと分かる形で残す"""
        self.open_session(dev_log=True)
        self.eng._translate_on = False
        self.eng._translators = [("fugumt", "en", lambda t: "good evening")]
        self.run_worker((9, "こんばんは"))
        ev = self.events()[-1]
        self.assertEqual(ev["out"], "good evening")
        self.assertFalse(ev["shown"])


if __name__ == "__main__":
    unittest.main()
