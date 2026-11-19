from transcriber.compare import PageCheck, check_page, merge_page
from transcriber.profiles import CJK, EDITORIAL


def merge(a, b, notation=EDITORIAL, c=None, drop=25):
    pc = PageCheck(1, a, b)
    return merge_page(a, b, pc, notation, drop, c), pc


def test_identical():
    out, pc = merge("The cat sat on the mat.", "The cat sat on the mat.")
    assert out == "The cat sat on the mat." and pc.conflicts == 0


def test_word_conflict():
    out, _ = merge("The cat sat on the mat.", "The cat sat on the hat.")
    assert out == "The cat sat on the {mat|hat}."


def test_missing_word_keeps_spacing_either_way():
    out, _ = merge("The sat on the mat.", "The cat sat on the mat.")
    assert out == "The {|cat }sat on the mat."
    assert out.replace("{|cat }", "cat ") == "The cat sat on the mat."
    assert out.replace("{|cat }", "") == "The sat on the mat."


def test_dropped_line_goes_on_its_own_line():
    out, pc = merge("First line.\nThird line.", "First line.\nSecond line was dropped by A here.\nThird line.")
    assert out == "First line.\n{+B: Second line was dropped by A here.}\nThird line."
    assert pc.dropped == 1


def test_dropped_last_line():
    out, _ = merge("Last line.", "Last line.\nA whole extra closing line at the end")
    assert out == "Last line.\n{+B: A whole extra closing line at the end}"


def test_uncertain_marker_kept_when_b_agrees_and_unwrapped_in_conflict():
    assert merge("Letter from [Lowell?] to Hughes.", "Letter from Lowell to Hughes.")[0] == "Letter from [Lowell?] to Hughes."
    assert merge("Letter from [Lowell?] to Hughes.", "Letter from Lovell to Hughes.")[0] == "Letter from {Lowell|Lovell} to Hughes."


def test_illegible_is_one_token():
    out, _ = merge("He wrote [illegible] yesterday.", "He wrote twice yesterday.")
    assert out == "He wrote {[illegible]|twice} yesterday."


def test_compatibility_forms_are_not_differences():
    out, pc = merge("ſo ﬁne", "so fine")
    assert pc.conflicts == 0 and out == "ſo ﬁne"


def test_third_model_votes():
    out, pc = merge("The cat sat on the mat.", "The cat sat on the hat.", c="The cat sat on the hat.")
    assert out == "The cat sat on the hat." and pc.voted == 1
    out, _ = merge("The cat sat on the mat.", "The cat sat on the hat.", c="The cat sat on the bat.")
    assert out == "The cat sat on the {mat|hat|bat}."
    out, _ = merge("First line.\nThird line.", "First line.\nSecond line was dropped by A here.\nThird line.",
                   c="First line.\nSecond line was dropped by A here.\nThird line.")
    assert "{+BC: Second line was dropped by A here.}" in out


def test_cjk_characters():
    assert merge("我们今天去学校。", "我们明天去学校。", CJK, drop=8)[0] == "我们〔今|明〕天去学校。"
    assert merge("这是〔总〕结。", "这是忌结。", CJK, drop=8)[0] == "这是〔总|忌〕结。"


def test_cjk_dropped_line_aligns_to_line_start():
    out, _ = merge("第一行。\n第三行。", "第一行。\n第二行被模型A漏掉了内容。\n第三行。", CJK, drop=8)
    assert out == "第一行。\n【补B:第二行被模型A漏掉了内容。】\n第三行。"


def test_mixed_script_words():
    out, _ = merge("Smith 来信说 telegraph 很重要", "Smith 来信说 telegram 很重要", CJK, drop=8)
    assert out == "Smith 来信说 〔telegraph|telegram〕 很重要"


def test_check_page_flags():
    pc = check_page(0, "Dear Sir,\nI am obliged.", "Dear Sir,\nI am most obliged.\nYours faithfully, Margaret Hale",
                    EDITORIAL, 0, threshold=0.9, drop_chars=25, conflict_rate=0.02)
    assert any("dropped" in f for f in pc.flags)
    pc = check_page(0, "I'm sorry, I cannot transcribe this.", "Real text here.", EDITORIAL, 0,
                    threshold=0.9, drop_chars=25, conflict_rate=0.02)
    assert any("refusal" in f for f in pc.flags)
