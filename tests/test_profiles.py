import pytest

from transcriber.auth import pkce_pair
from transcriber.profiles import CJK, EDITORIAL, PROFILES, build_prompt, prompt_id
from transcriber.review import segments
from transcriber.sources import natural_key


@pytest.mark.parametrize("profile", list(PROFILES))
def test_prompts_build(profile):
    p = build_prompt(profile, language="German", layout="lines", diplomatic=True)
    assert "Never translate" in p and "German" in p and "line break" in p
    assert ("□" in p) == (profile == "cjk")
    assert "□" in build_prompt(profile, notation="cjk") and "[illegible]" in build_prompt(profile, notation="editorial")


def test_prompt_id_changes_with_prompt():
    assert prompt_id(build_prompt("printed")) != prompt_id(build_prompt("handwritten"))


def test_editorial_segments():
    segs = segments("A {mat|hat} and [Lowell?] wrote [illegible]. {+B: a line}{|cat }{^not}", EDITORIAL)
    kinds = [s["t"] for s in segs]
    assert kinds == ["text", "conf", "text", "unc", "text", "unc", "text", "drop", "conf", "text"]
    assert segs[1]["opts"] == ["mat", "hat"] and segs[3]["s"] == "Lowell" and segs[5]["s"] == ""
    assert segs[7] == {"t": "drop", "s": "a line", "who": "B"} and segs[8]["opts"] == ["", "cat "]
    assert segs[9]["s"] == "{^not}"  # a handwriting insertion is text, not a review marker


def test_cjk_segments_and_truncation_notice():
    segs = segments("这〔总|忌〕是〔字〕□【补BC:漏行】〔转录在此截断，后文缺失〕", CJK)
    assert [s["t"] for s in segs] == ["text", "conf", "text", "unc", "unc", "drop", "text"]
    assert CJK.count_uncertain("〔转录在此截断，后文缺失〕") == 0


def test_pkce_pair_is_s256():
    import base64
    import hashlib
    v, c = pkce_pair()
    assert c == base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    assert "=" not in v and 43 <= len(v) <= 128


def test_natural_order_of_photos():
    from pathlib import Path
    names = ["IMG_10.jpg", "IMG_2.jpg", "IMG_1.jpg"]
    assert sorted((Path(n) for n in names), key=natural_key) == [Path("IMG_1.jpg"), Path("IMG_2.jpg"), Path("IMG_10.jpg")]
