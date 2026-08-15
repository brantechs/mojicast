"""
単語登録（読み付き）

語彙ファイルの各行は  表記,読み,スコア  （読み・スコアは省略可）:
    癒色えも,いろえも
    えもてぃっく              ← 読み省略時は表記をそのまま読みとして使う
    リーゾンスピーチ,りーぞんすぴーち,3.0
    癒色えも,意識へモ／意識エモ   ← 読み（誤変換形）は「／」区切りで複数OK

この語彙から
  1) sherpa-onnx 用ホットワードファイル（"読み" で認識を誘導）を生成
  2) 認識後に  読み→表記  へ置換する関数
を作る。

なぜ読みが要るか:
  k2 の音響モデルは「音」に対応するトークンしか出さない。漢字表記(癒色えも)を
  そのまま登録しても、その音に漢字トークンの音響スコアが無いためブーストしても
  選ばれない。そこで「読み(いろえも)」でホットボーストして認識を安定させ、
  出てきた読みを表記(癒色えも)へ後段で置換する。

なぜ読みが複数要るか:
  誤変換の形は認識モデルごとに違う（k2=「意識へモ」/ SenseVoice=「意識エモ」等）。
  認識誘導が使えない多言語モデルでは置換が唯一の対策なので、モデルごとの
  誤変換形を並記できるようにする。
"""
import os
import re
import tempfile


def _split_readings(reading: str):
    """読み欄を「／」または「/」で複数の形に分割する（空要素は除く）"""
    return [r.strip() for r in re.split(r"[／/]", reading or "") if r.strip()]


# かなの表記ゆれ（ひらがな⇄カタカナ）を吸収するための対応表。
# ぁ(U+3041)〜ゖ(U+3096) と ァ(U+30A1)〜ヶ(U+30F6) は 0x60 ずれの同順。
# 「ー」「・」等はどちらの表記でも共通なのでそのまま通す。
_KANA_SHIFT = 0x60
_KANA_COMMON = "ー゛゜・ｰ"


def _is_kana(s: str) -> bool:
    """語全体がかな（＋長音符など）だけで出来ているか"""
    if not s:
        return False
    return all("ぁ" <= c <= "ゖ" or "ァ" <= c <= "ヶ"
               or c in _KANA_COMMON for c in s)


def _to_hiragana(s: str) -> str:
    return "".join(chr(ord(c) - _KANA_SHIFT) if "ァ" <= c <= "ヶ"
                   else c for c in s)


def _to_katakana(s: str) -> str:
    return "".join(chr(ord(c) + _KANA_SHIFT) if "ぁ" <= c <= "ゖ"
                   else c for c in s)


def expand_kana(forms):
    """かな語の表記ゆれ（ひらがな⇄カタカナ）を足した一覧を返す（順序保持・重複なし）

    「ぶらんち」だけ登録すれば認識結果の「ブランチ」も同じ語として扱える。
    漢字・英数字を含む語は変換しない（誤爆を避けるため、全部かなの語だけ）。
    """
    out = []
    for f in forms:
        for v in (f, _to_hiragana(f), _to_katakana(f)) if _is_kana(f) else (f,):
            if v and v not in out:
                out.append(v)
    return out


def parse_vocab(path: str):
    """語彙ファイルを (表記, 読み, スコア) のリストに読み込む"""
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(",")]
            surface = parts[0]
            reading = parts[1] if len(parts) > 1 and parts[1] else surface
            score = parts[2] if len(parts) > 2 and parts[2] else None
            entries.append((surface, reading, score))
    return entries


def write_hotwords(entries, out_path: str | None = None) -> str:
    """entries から sherpa-onnx 用ホットワードファイル（読みベース）を書き出す"""
    if out_path is None:
        fd, out_path = tempfile.mkstemp(prefix="hotwords_", suffix=".txt")
        os.close(fd)
    with open(out_path, "w", encoding="utf-8") as f:
        for _surface, reading, score in entries:
            # かな語はひらがな形・カタカナ形の両方を誘導対象にする。
            # モデルがどちらの形で出すかは語によって違うため（ぶらんち/ブランチ）
            for r in expand_kana(_split_readings(reading)):
                line = r
                if score:
                    line += f" :{score}"
                f.write(line + "\n")
    return out_path


def build_replacer(entries):
    """読み→表記 の置換関数を返す（読みが長いものから先に置換）

    - 読み欄の「／」区切り複数形にそれぞれ対応
    - SenseVoice等が単語の途中に句読点を挟むケース（「意識、エモ」）にも
      当たるよう、文字間に句読点・空白を許す寛容マッチも行う
    - かな語はひらがな⇄カタカナの表記ゆれも同じ表記へ寄せる。認識モデルが
      かな語をどちらで出すかは選べないため（表記「ぶらんち」でも認識結果は
      「ブランチ」になる）、読みを書かなくても表記に統一される。
      これでエフェクト単語（表記で照合）も取りこぼさない。
    """
    pairs = []
    for surface, reading, _ in entries:
        # 読みだけでなく表記自身のかな別形も拾う（表記「ぶらんち」⇄「ブランチ」）
        for r in expand_kana(_split_readings(reading) + [surface]):
            if r != surface:
                pairs.append((r, surface))
    if not pairs:
        return lambda text: text
    # 表記そのものも「自分自身への置換」として登録する（保護パターン）。
    # 本文に正しい表記が既に出ているとき、その一部に短い読みがマッチして
    # 壊れるのを防ぐ（例: 本文の「意識エモい系」を読み「意識エモ」が食う）。
    for surface, _reading, _ in entries:
        pairs.append((surface, surface))
    # 全パターンを1本の正規表現に束ねて「1パスで同時置換」する。
    # 逐次replaceだと、置換結果（表記）の中に別エントリの読みが含まれるとき
    # 連鎖して壊れる（例: →意識エモい系 の中の「意識エモ」が再置換される）。
    # 長い読みを先に並べ、同じ開始位置では最長のパターンが勝つようにする。
    pairs.sort(key=lambda x: len(x[0]), reverse=True)
    parts = []
    surfaces = []
    for i, (r, surface) in enumerate(pairs):
        parts.append(f"(?P<g{i}>"
                     + "[、。，．・\\s]*".join(map(re.escape, r)) + ")")
        surfaces.append(surface)
    rx = re.compile("|".join(parts))

    def replace(text: str) -> str:
        return rx.sub(lambda m: surfaces[int(m.lastgroup[1:])], text)

    return replace
