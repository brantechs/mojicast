"""AI辞書づくり用のログ（logs/daily/ の日次ローテーションと export のzip）"""
import os
import shutil
import tempfile
import unittest
import zipfile
from datetime import datetime

import app_server
import engine
import wordstore


class FrozenDatetime(datetime):
    """now() だけ固定する差し替え用（fromtimestamp 等は本物のまま）"""

    _now = datetime(2026, 8, 14, 21, 0, 0)

    @classmethod
    def now(cls, tz=None):
        return cls._now


class DailyLogRotationTests(unittest.TestCase):
    """logs/daily/日番号.txt は同じ月なら追記・別の月なら上書きで一巡する"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mojicast_daily_")
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

    def at(self, *args):
        FrozenDatetime._now = datetime(*args)

    def daily_path(self, day):
        return os.path.join(self.tmp, "logs", "daily", f"{day:02d}.txt")

    def read_daily(self, day):
        with open(self.daily_path(day), encoding="utf-8") as f:
            return f.read()

    def write_daily(self, day, text, mtime):
        path = self.daily_path(day)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        ts = mtime.timestamp()
        os.utime(path, (ts, ts))

    def session(self, *when):
        """指定時刻でセッションを開き直す（設定は保存ON）"""
        self.at(*when)
        self.eng._open_log({"save_log": True})

    # --- テスト ---

    def test_same_month_appends_across_sessions(self):
        self.session(2026, 8, 14, 21, 0, 0)
        self.eng._log_final("こんばんは")
        self.eng._close_log()
        self.session(2026, 8, 14, 22, 30, 0)
        self.eng._log_final("ただいま")
        self.eng._close_log()

        body = self.read_daily(14)
        self.assertEqual(body.count("\n"), 2)
        self.assertIn("[2026-08-14 21:00:00] こんばんは\n", body)
        self.assertIn("[2026-08-14 22:30:00] ただいま\n", body)

    def test_other_month_truncates_before_writing(self):
        self.write_daily(14, "[2026-07-14 12:00:00] 先月の分\n",
                         datetime(2026, 7, 14, 12, 0, 0))
        self.session(2026, 8, 14, 9, 0, 0)
        self.eng._log_final("今月の分")
        self.eng._close_log()

        body = self.read_daily(14)
        self.assertNotIn("先月の分", body)
        self.assertEqual(body, "[2026-08-14 09:00:00] 今月の分\n")

    def test_same_month_existing_file_is_kept(self):
        self.write_daily(14, "[2026-08-14 08:00:00] 朝の分\n",
                         datetime(2026, 8, 14, 8, 0, 0))
        self.session(2026, 8, 14, 20, 0, 0)
        self.eng._log_final("夜の分")
        self.eng._close_log()

        body = self.read_daily(14)
        self.assertIn("朝の分", body)
        self.assertIn("夜の分", body)

    def test_midnight_rollover_switches_file(self):
        self.session(2026, 8, 14, 23, 59, 50)
        self.eng._log_final("きょうの発言")
        self.at(2026, 8, 15, 0, 0, 10)
        self.eng._log_final("あしたの発言")
        self.eng._close_log()

        self.assertEqual(self.read_daily(14),
                         "[2026-08-14 23:59:50] きょうの発言\n")
        self.assertEqual(self.read_daily(15),
                         "[2026-08-15 00:00:10] あしたの発言\n")

    def test_speaker_is_recorded_like_session_log(self):
        self.session(2026, 8, 14, 21, 0, 0)
        self.eng._log_final("よろしく", "ゲスト")
        self.eng._close_log()

        self.assertEqual(self.read_daily(14),
                         "[2026-08-14 21:00:00] [ゲスト] よろしく\n")

    def test_session_log_still_written(self):
        self.session(2026, 8, 14, 21, 0, 0)
        self.eng._log_final("こんばんは")
        self.eng._close_log()

        d = os.path.join(self.tmp, "logs", "2026-08-14")
        self.assertEqual(os.listdir(d), ["2026-08-14_210000.log"])
        with open(os.path.join(d, "2026-08-14_210000.log"),
                  encoding="utf-8") as f:
            self.assertEqual(f.read(), "[2026-08-14 21:00:00] こんばんは\n")

    def test_save_log_off_writes_nothing(self):
        self.at(2026, 8, 14, 21, 0, 0)
        self.eng._open_log({"save_log": False})
        self.eng._log_final("記録しない")
        self.eng._close_log()

        self.assertFalse(os.path.exists(os.path.join(self.tmp, "logs")))

    def test_unwritable_daily_dir_does_not_raise(self):
        # logs/daily が（ファイルとして）作れない状態でも認識は続行する
        os.makedirs(os.path.join(self.tmp, "logs"), exist_ok=True)
        with open(os.path.join(self.tmp, "logs", "daily"), "w",
                  encoding="utf-8") as f:
            f.write("これはフォルダではない")
        self.session(2026, 8, 14, 21, 0, 0)
        self.eng._log_final("落ちない")
        self.eng._close_log()


class DictLogExportTests(unittest.TestCase):
    """export_dict_logs() は日次ログとAI用手順書をzipにまとめる"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mojicast_export_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        orig_base = app_server.DATA_BASE
        app_server.DATA_BASE = self.tmp
        self.addCleanup(setattr, app_server, "DATA_BASE", orig_base)
        # 書き出し先は mojipack と同じ data/export（wordstore 経由）
        orig_data = wordstore.DATA
        wordstore.DATA = os.path.join(self.tmp, "data")
        self.addCleanup(setattr, wordstore, "DATA", orig_data)

    def export_dir(self):
        return os.path.join(self.tmp, "data", "export")

    def write_daily(self, name, text):
        d = os.path.join(self.tmp, "logs", "daily")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(text)

    def test_no_daily_logs_returns_none(self):
        self.assertEqual(app_server.export_dict_logs(), (None, 0))
        self.assertFalse(os.path.exists(self.export_dir()))

    def test_empty_daily_logs_are_skipped(self):
        self.write_daily("01.txt", "")
        self.assertEqual(app_server.export_dict_logs(), (None, 0))

    def test_zip_holds_prompt_and_non_empty_logs(self):
        self.write_daily("01.txt", "[2026-08-01 10:00:00] ついたちの発言\n")
        self.write_daily("02.txt", "")          # 空 → 同梱しない
        self.write_daily("03.txt", "[2026-08-03 10:00:00] みっかの発言\n")

        path, count = app_server.export_dict_logs()
        self.assertEqual(count, 2)
        self.assertEqual(os.path.dirname(path), self.export_dir())
        base = os.path.basename(path)
        self.assertTrue(base.startswith("dict_logs_"), base)
        self.assertTrue(base.endswith(".zip"), base)

        with zipfile.ZipFile(path) as z:
            names = sorted(z.namelist())
            self.assertEqual(names, [app_server.AI_PROMPT_NAME,
                                     "logs/01.txt", "logs/03.txt"])
            self.assertEqual(z.read("logs/01.txt").decode("utf-8"),
                             "[2026-08-01 10:00:00] ついたちの発言\n")
            prompt = z.read(app_server.AI_PROMPT_NAME).decode("utf-8")
        # 手順書はホットワードの書式をAIに教えるためのもの
        self.assertIn("hotwords.txt", prompt)
        self.assertIn("表記,読み,スコア", prompt)

    def test_prompt_is_refreshed_in_logs_root(self):
        self.write_daily("01.txt", "[2026-08-01 10:00:00] 発言\n")
        prompt_path = os.path.join(self.tmp, "logs", app_server.AI_PROMPT_NAME)
        with open(prompt_path, "w", encoding="utf-8") as f:
            f.write("ふるい内容")

        app_server.export_dict_logs()
        with open(prompt_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), app_server.AI_PROMPT_TEXT)

    def test_daily_dir_name_matches_engine(self):
        # app_server 側は engine を import せず名前だけ合わせている
        self.assertEqual(app_server.DAILY_LOG_DIR_NAME,
                         engine.DAILY_LOG_DIR_NAME)


if __name__ == "__main__":
    unittest.main()
