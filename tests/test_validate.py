import pytest

from konyvhang.validate import ValidationError, parse_and_check

SOURCE = {"1": 'Hello <em n="1">world</em>', "2": "Bye"}


def test_valid_response_with_noise_and_bare_ampersand() -> None:
    text = 'Here you go:\n```\n<seg id="1">Szia <em n="1">világ</em></seg>\n<seg id="2">Tom & Mary</seg>\n```'
    result = parse_and_check(text, SOURCE)
    assert result["2"].text == "Tom & Mary"


def test_missing_segment() -> None:
    with pytest.raises(ValidationError, match="Hiányzó szegmensek: 2"):
        parse_and_check('<seg id="1">Szia <em n="1">világ</em></seg>', SOURCE)


def test_changed_inline_tag() -> None:
    with pytest.raises(ValidationError, match="1 szegmens címkéi"):
        parse_and_check('<seg id="1">Szia <i n="1">világ</i></seg><seg id="2">Viszlát</seg>', SOURCE)


def test_dropped_inline_tag() -> None:
    with pytest.raises(ValidationError, match="1 szegmens címkéi"):
        parse_and_check('<seg id="1">Szia világ</seg><seg id="2">Viszlát</seg>', SOURCE)


def test_malformed_xml() -> None:
    with pytest.raises(ValidationError, match="nem jól formált"):
        parse_and_check('<seg id="1">Szia <em n="1">világ</seg><seg id="2">x</seg>', SOURCE)


def test_no_segments() -> None:
    with pytest.raises(ValidationError, match="nincs <seg>"):
        parse_and_check("Sorry, I cannot.", SOURCE)


def test_parse_json_ignores_text_after_the_value() -> None:
    from konyvhang.llm import parse_json

    assert parse_json('Íme:\n["a", "b [x]"]\nMegjegyzés: [már nem elérhető]', "[") == ["a", "b [x]"]
    assert parse_json('```json\n{"k": 1}\n```', "{") == {"k": 1}


def test_provider_defaults_and_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    from konyvhang import llm

    assert llm.default_model("claude", None) == "opus"
    assert llm.default_model("anthropic", None) == "claude-opus-5-5"
    assert llm.default_model("codex", None) is None  # Codex picks its own default
    assert llm.default_model("openrouter", "some/model") == "some/model"
    with pytest.raises(llm.LLMError, match="--model"):
        llm.default_model("openai", None)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(llm.LLMError, match="OPENROUTER_API_KEY"):
        llm.check_ready("openrouter")
    with pytest.raises(llm.LLMError, match="Ismeretlen"):
        llm.caller("gemini")
