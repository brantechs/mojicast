import threading
import time
import unittest
import queue

from engine import (
    AUDIO_QUEUE_MAX_BLOCKS,
    AUDIO_QUEUE_RECOVER_BLOCKS,
    SAMPLE_RATE,
    TRANSLATION_QUEUE_MAX_ITEMS,
    WINDOW_SIZE,
    CaptionEngine,
    _offer_bounded_latest,
)


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class ControlledEngine(CaptionEngine):
    """モデル・音声デバイスを使わずライフサイクルだけを試すエンジン。"""

    def __init__(self, *, block_load=False, fail_load_times=0,
                 stream_error=None, stream_exits=False, block_notice=False):
        self.events = []
        self._events_lock = threading.Lock()
        super().__init__(on_state=self._record_state)
        self.block_load = block_load
        self.fail_load_times = fail_load_times
        self.initial_stream_error = stream_error
        self.stream_exits = stream_exits
        self.block_notice = block_notice
        self.notice_entered = threading.Event()
        self.release_notice = threading.Event()
        self.load_entered = threading.Event()
        self.release_load = threading.Event()
        self.stream_started = threading.Event()
        self.load_calls = 0
        self.stream_calls = 0

    def _record_state(self, state, detail=""):
        with self._events_lock:
            self.events.append((state, detail))
        if self.block_notice and state == "loading":
            self.notice_entered.set()
            self.release_notice.wait(2)

    def _load(self, _cfg):
        self.load_calls += 1
        self.load_entered.set()
        if self.block_load:
            self.release_load.wait(2)
        if self.fail_load_times:
            self.fail_load_times -= 1
            raise RuntimeError("test load failure")

    def _open_log(self, _cfg):
        self._logf = None

    def _build_masker(self, _cfg):
        return None

    def _build_glossary(self, _cfg):
        return None

    def _resolve_sources(self, _cfg):
        return [(None, "", True)]

    def _stream_loop(self, _cfg, _device, _speaker, _primary):
        self.stream_calls += 1
        self.stream_started.set()
        if self.initial_stream_error:
            self._stream_error = self.initial_stream_error
            self._stop.set()
            return
        if self.stream_exits:
            return
        self._stop.wait(2)


class CaptionEngineLifecycleTests(unittest.TestCase):
    def tearDown(self):
        engine = getattr(self, "engine", None)
        if engine is not None:
            if hasattr(engine, "release_notice"):
                engine.release_notice.set()
            if hasattr(engine, "release_load"):
                engine.release_load.set()
            engine.stop(timeout=1)

    def test_concurrent_start_requests_create_only_one_session(self):
        self.engine = ControlledEngine(block_load=True)
        callers = 16
        barrier = threading.Barrier(callers)
        results = []
        result_lock = threading.Lock()

        def request_start():
            barrier.wait()
            accepted = self.engine.start({"request": "same"})
            with result_lock:
                results.append(accepted)

        threads = [threading.Thread(target=request_start) for _ in range(callers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=1)

        self.assertTrue(self.engine.load_entered.wait(1))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), callers - 1)
        self.assertEqual(self.engine.load_calls, 1)
        self.assertEqual(self.engine.lifecycle_state, "starting")

    def test_stop_during_load_blocks_restart_and_never_opens_audio(self):
        self.engine = ControlledEngine(block_load=True)
        self.assertTrue(self.engine.start({}))
        self.assertTrue(self.engine.load_entered.wait(1))

        self.assertFalse(self.engine.stop(timeout=0.01))
        self.assertEqual(self.engine.lifecycle_state, "stopping")
        self.assertFalse(self.engine.start({}))
        self.assertEqual(self.engine.load_calls, 1)

        self.engine.release_load.set()
        self.assertTrue(wait_until(
            lambda: self.engine.lifecycle_state == "stopped"
            and self.engine._thread is None
        ))
        self.assertEqual(self.engine.stream_calls, 0)
        states = [state for state, _detail in self.engine.events]
        self.assertIn("stopping", states)
        self.assertNotIn("running", states)

    def test_start_stop_race_before_worker_release_cannot_lose_thread(self):
        self.engine = ControlledEngine(block_load=True, block_notice=True)
        first_result = []
        duplicate_result = []
        stop_result = []

        first = threading.Thread(
            target=lambda: first_result.append(self.engine.start({}))
        )
        first.start()
        self.assertTrue(self.engine.notice_entered.wait(1))

        duplicate = threading.Thread(
            target=lambda: duplicate_result.append(self.engine.start({}))
        )
        stopper = threading.Thread(
            target=lambda: stop_result.append(self.engine.stop(timeout=0.05))
        )
        duplicate.start()
        stopper.start()
        self.engine.release_notice.set()

        first.join(timeout=1)
        duplicate.join(timeout=1)
        stopper.join(timeout=1)
        self.assertEqual(first_result, [True])
        self.assertEqual(duplicate_result, [False])
        self.assertTrue(self.engine.load_entered.wait(1))
        self.assertEqual(self.engine.load_calls, 1)

        self.engine.release_load.set()
        self.assertTrue(wait_until(
            lambda: self.engine.lifecycle_state == "stopped"
            and self.engine._thread is None
        ))

    def test_restart_is_allowed_after_stopped_session_has_fully_exited(self):
        self.engine = ControlledEngine(block_load=True)
        self.assertTrue(self.engine.start({}))
        self.assertTrue(self.engine.load_entered.wait(1))
        self.assertFalse(self.engine.stop(timeout=0.01))
        self.engine.release_load.set()
        self.assertTrue(wait_until(lambda: self.engine._thread is None))

        self.engine.load_entered.clear()
        self.assertTrue(self.engine.start({}))
        self.assertTrue(self.engine.stream_started.wait(1))
        self.assertTrue(wait_until(
            lambda: self.engine.lifecycle_state == "running"
        ))
        self.assertEqual(self.engine.load_calls, 2)
        self.assertTrue(self.engine.stop(timeout=1))
        self.assertTrue(wait_until(
            lambda: self.engine.lifecycle_state == "stopped"
        ))

    def test_load_failure_enters_error_and_can_be_retried(self):
        self.engine = ControlledEngine(fail_load_times=1)
        self.assertTrue(self.engine.start({}))
        self.assertTrue(wait_until(
            lambda: self.engine.lifecycle_state == "error"
            and self.engine._thread is None
        ))
        self.assertFalse(self.engine.running)

        self.assertTrue(self.engine.start({}))
        self.assertTrue(wait_until(
            lambda: self.engine.lifecycle_state == "running"
        ))
        self.assertEqual(self.engine.load_calls, 2)
        self.assertTrue(self.engine.stop(timeout=1))

    def test_stream_start_failure_enters_error(self):
        self.engine = ControlledEngine(stream_error="test input failure")
        self.assertTrue(self.engine.start({}))
        self.assertTrue(wait_until(
            lambda: self.engine.lifecycle_state == "error"
            and self.engine._thread is None
        ))
        self.assertFalse(self.engine.running)
        self.assertTrue(any(
            state == "error" and detail == "test input failure"
            for state, detail in self.engine.events
        ))

    def test_natural_stream_end_returns_to_stopped(self):
        self.engine = ControlledEngine(stream_exits=True)
        self.assertTrue(self.engine.start({}))
        self.assertTrue(wait_until(
            lambda: self.engine.lifecycle_state == "stopped"
            and self.engine._thread is None
        ))
        self.assertFalse(self.engine.running)

    def test_stop_is_idempotent_before_and_after_a_session(self):
        self.engine = ControlledEngine()
        self.assertTrue(self.engine.stop(timeout=0))
        self.assertTrue(self.engine.stop(timeout=0))
        self.assertEqual(self.engine.events, [])

        self.assertTrue(self.engine.start({}))
        self.assertTrue(wait_until(
            lambda: self.engine.lifecycle_state == "running"
        ))
        self.assertTrue(self.engine.stop(timeout=1))
        event_count = len(self.engine.events)
        self.assertTrue(self.engine.stop(timeout=0))
        self.assertEqual(len(self.engine.events), event_count)

    def test_bounded_queue_keeps_normal_items_unchanged(self):
        q = queue.Queue(maxsize=4)
        for value in range(4):
            self.assertEqual(_offer_bounded_latest(q, value, 1), 0)
        self.assertEqual(list(q.queue), [0, 1, 2, 3])

    def test_bounded_queue_discards_oldest_only_after_limit(self):
        q = queue.Queue(maxsize=4)
        for value in range(4):
            q.put_nowait(value)
        dropped = _offer_bounded_latest(q, 4, 1)
        self.assertEqual(dropped, 3)
        self.assertEqual(list(q.queue), [3, 4])

    def test_production_queue_limits_leave_large_normal_headroom(self):
        buffered_audio_sec = (
            AUDIO_QUEUE_MAX_BLOCKS * WINDOW_SIZE / SAMPLE_RATE
        )
        self.assertGreaterEqual(buffered_audio_sec, 29.9)
        self.assertLess(AUDIO_QUEUE_RECOVER_BLOCKS, AUDIO_QUEUE_MAX_BLOCKS)
        self.assertGreaterEqual(TRANSLATION_QUEUE_MAX_ITEMS, 100)

    def test_translation_stop_discards_backlog_and_joins_worker(self):
        translated = []
        translating = threading.Event()
        release = threading.Event()
        self.engine = CaptionEngine(
            on_translation=lambda fid, lang, text:
                translated.append((fid, lang, text))
        )

        def slow_translate(text):
            translating.set()
            release.wait(1)
            return "tr:" + text

        self.engine._translators = [("fugumt", "en", slow_translate)]
        self.engine._translate_on = True
        self.engine._tq = queue.Queue(maxsize=8)
        self.engine._tworker = threading.Thread(
            target=self.engine._translate_loop, daemon=True
        )
        self.engine._tworker.start()
        self.engine._tq.put_nowait((1, "current"))
        self.assertTrue(translating.wait(1))
        for fid in range(2, 8):
            self.engine._tq.put_nowait((fid, f"pending-{fid}"))

        stopped = threading.Event()
        stopper = threading.Thread(
            target=lambda: (self.engine._stop_translate_worker(), stopped.set())
        )
        stopper.start()
        time.sleep(0.03)
        self.assertFalse(stopped.is_set())
        self.assertLessEqual(self.engine._tq.qsize(), 1)

        release.set()
        stopper.join(timeout=1)
        self.assertTrue(stopped.is_set())
        self.assertIsNone(self.engine._tworker)
        self.assertIsNone(self.engine._tq)
        self.assertEqual(translated, [])

    def test_translation_worker_emits_each_target_language(self):
        translated = []
        done = threading.Event()
        self.engine = CaptionEngine(
            on_translation=lambda fid, lang, text:
                (translated.append((fid, lang, text)),
                 done.set() if lang == "zh" else None)
        )
        self.engine._translators = [
            ("fugumt", "en", lambda t: "en:" + t),
            ("m2m", "zh", lambda t: "zh:" + t),
        ]
        self.engine._translate_on = True
        self.engine._tq = queue.Queue(maxsize=8)
        self.engine._tworker = threading.Thread(
            target=self.engine._translate_loop, daemon=True
        )
        self.engine._tworker.start()
        self.engine._tq.put_nowait((1, "こんにちは"))
        self.assertTrue(done.wait(1))
        self.engine._stop_translate_worker()
        self.assertEqual(translated, [(1, "en", "en:こんにちは"),
                                      (1, "zh", "zh:こんにちは")])

    def test_translation_worker_emits_three_target_languages(self):
        translated = []
        done = threading.Event()
        self.engine = CaptionEngine(
            on_translation=lambda fid, lang, text:
                (translated.append((fid, lang, text)),
                 done.set() if lang == "ko" else None)
        )
        self.engine._translators = [
            ("fugumt", "en", lambda t: "en:" + t),
            ("opencc", "zh_tw", lambda t: "zh_tw:" + t),
            ("m2m", "ko", lambda t: "ko:" + t),
        ]
        self.engine._translate_on = True
        self.engine._tq = queue.Queue(maxsize=8)
        self.engine._tworker = threading.Thread(
            target=self.engine._translate_loop, daemon=True
        )
        self.engine._tworker.start()
        self.engine._tq.put_nowait((1, "こんにちは"))
        self.assertTrue(done.wait(1))
        self.engine._stop_translate_worker()
        # 第1→第2→第3翻訳先の順（＝設定順）に通知される
        self.assertEqual(translated, [(1, "en", "en:こんにちは"),
                                      (1, "zh_tw", "zh_tw:こんにちは"),
                                      (1, "ko", "ko:こんにちは")])


class TranslatePlansTest(unittest.TestCase):
    """_translate_plans: 第1〜第3翻訳先から翻訳経路を決める判定"""

    def setUp(self):
        self.engine = CaptionEngine()

    def plans(self, **cfg):
        return self.engine._translate_plans({"translate": True, **cfg})

    def test_disabled_returns_empty(self):
        self.assertEqual(
            self.engine._translate_plans({"translate": False}), ())

    def test_single_target_default_en(self):
        self.assertEqual(self.plans(), (("fugumt", "ja", "en"),))

    def test_second_target_appends_plan(self):
        self.assertEqual(
            self.plans(translate_lang="en", translate_lang2="zh_tw"),
            (("fugumt", "ja", "en"), ("m2m", "ja", "zh_tw")))

    def test_duplicate_targets_collapse(self):
        self.assertEqual(
            self.plans(translate_lang="en", translate_lang2="en"),
            (("fugumt", "ja", "en"),))

    def test_third_target_appends_plan(self):
        self.assertEqual(
            self.plans(translate_lang="en", translate_lang2="zh_tw",
                       translate_lang3="ko"),
            (("fugumt", "ja", "en"), ("m2m", "ja", "zh_tw"),
             ("m2m", "ja", "ko")))

    def test_third_target_collapses_when_duplicated(self):
        # 3つ目が1つ目・2つ目と重複しても経路は増えない
        self.assertEqual(
            self.plans(translate_lang="en", translate_lang2="ko",
                       translate_lang3="en"),
            (("fugumt", "ja", "en"), ("m2m", "ja", "ko")))

    def test_empty_second_target_keeps_single(self):
        self.assertEqual(
            self.plans(translate_lang="zh", translate_lang2=""),
            (("m2m", "ja", "zh"),))

    def test_empty_second_target_does_not_drop_third(self):
        # 2つ目だけ「追加しない」でも、3つ目は設定順で生きる
        self.assertEqual(
            self.plans(translate_lang="en", translate_lang2="",
                       translate_lang3="zh"),
            (("fugumt", "ja", "en"), ("m2m", "ja", "zh")))

    def test_same_as_source_skipped_per_target(self):
        # 中国語認識中: 第1(zh)は原文と同じでスキップ、第2(en)だけ生きる
        self.assertEqual(
            self.plans(asr_model="sensevoice", asr_lang="zh",
                       translate_lang="zh", translate_lang2="en"),
            (("m2m", "zh", "en"),))

    def test_opencc_route_for_zh_regional(self):
        self.assertEqual(
            self.plans(asr_model="sensevoice", asr_lang="zh",
                       translate_lang="zh_tw", translate_lang2="ja"),
            (("opencc", "zh", "zh_tw"), ("m2m", "zh", "ja")))


if __name__ == "__main__":
    unittest.main()
