from gapp.textutil import (
    canonical_url, clean_text, detect_rubric, is_russian, make_summary, similarity,
    slugify, title_hash, title_tokens,
)


def test_canonical_url_strips_tracking_and_amp():
    assert canonical_url("http://M.Example.az/news/1/?utm_source=tg&id=5#top") == "https://example.az/news/1?id=5"
    assert canonical_url("https://example.az/news/1/amp/") == "https://example.az/news/1"
    assert canonical_url("javascript:alert(1)") == ""


def test_clean_text_removes_markup():
    assert clean_text("<p>Привет,&nbsp;<b>мир</b></p><script>x</script>") == "Привет, мир"  # содержимое script отбрасывается


def test_summary_is_limited():
    text = make_summary("слово " * 200)
    assert len(text) <= 301 and text.endswith("…")


def test_language_filter():
    assert is_russian("В Баку открыли новую дорогу")
    assert not is_russian("Bakıda yeni yol açıldı")
    assert not is_russian("New road opened in Baku")
    assert not is_russian("Bakıda в Баку şəhər открыли дорогу")


def test_title_hash_ignores_punctuation_and_case():
    assert title_hash("В Баку открыли дорогу!") == title_hash("в баку — открыли дорогу")


def test_similar_titles():
    a = title_tokens("Центробанк Азербайджана объявил курс маната на завтра")
    b = title_tokens("Центробанк объявил официальный курс маната на завтра")
    c = title_tokens("Сборная Азербайджана назвала состав на матч")
    assert similarity(a, b) >= 0.5
    assert similarity(a, c) < 0.5


def test_rubric_and_slug():
    assert detect_rubric(["Спорт"]) == "sport"
    assert detect_rubric([], "https://x.az/economy/123") == "ekonomika"
    assert detect_rubric([], "https://x.az/123") == "raznoe"
    assert slugify("В Баку открыли дорогу!") == "v-baku-otkryli-dorogu"


def test_clean_text_drops_script_and_style_content():
    from gapp.textutil import clean_text

    assert clean_text("До <script>alert(1)</script><style>p{color:red}</style> <b>после</b>") == "До после"
