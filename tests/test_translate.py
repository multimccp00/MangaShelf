"""Text filters of the machine-translation pipeline (no OCR/LLM needed)."""
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "web")]
import translate as mt  # noqa: E402


def test_romanized_output_is_caught_but_english_and_names_pass():
    assert mt.romanized("ありがとうございます", "Arigatou gozaimasu")
    assert mt.romanized("おはようございます", "Ohayou gozaimasu ne")
    assert not mt.romanized("ご主人様", "Take more time here, master")
    assert not mt.romanized("田中", "Publisher: Tanaka (Yamada Books)")
    assert not mt.romanized("你好", "Arigatou gozaimasu")   # only Japanese sources


def test_ocr_debris_is_stripped_and_junk_dropped():
    assert mt._clean_line("はっはっ7", "ja") == "はっはっ"
    assert mt._clean_line("XおViあ", "ja") == "おあ"
    assert mt._clean_line("STUDIO PRESENTS", "ja") == "STUDIO PRESENTS"   # no CJK: left alone
    assert mt._is_junk("无业", "ja", False)             # Chinese-only glyphs on a Japanese page
    assert mt._is_junk("ー!!", "ja", True)               # nothing but punctuation
    assert not mt._is_junk("数分後", "ja", False)        # a real 3-kanji caption
    assert not mt._is_junk("无业これこれっ", "ja", False)  # has kana: real text


def test_series_language_from_folder_name():
    assert mt.language_from_title("[Group] Title [Japanese]") == "ja"
    assert mt.language_from_title("Series-Chinese-Title") == "zh"
    assert mt.language_from_title("Plain Title") == ""
