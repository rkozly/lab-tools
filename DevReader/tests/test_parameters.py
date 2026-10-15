import pytest

from devreader.config.parameters import ParameterDef, ParamType
from devreader.core.errors import ConfigValidationError


def test_from_dict_minimal():
    p = ParameterDef.from_dict({"section": "qcawifi", "key": "rf_freq1_KHz", "label": "RF Frequency 1 (KHz)"})
    assert p.id == "qcawifi.rf_freq1_KHz"
    assert p.type == ParamType.STRING


def test_int_validation():
    p = ParameterDef.from_dict({"section": "s", "key": "k", "label": "K", "type": "int", "min": 0, "max": 100})
    assert p.validate(" 042 ") == "42"
    for bad in ("", "abc", "1.5", "-1", "101", "1'"):
        with pytest.raises(ConfigValidationError):
            p.validate(bad)


def test_choice_and_bool():
    c = ParameterDef.from_dict({"section": "s", "key": "k", "label": "K", "type": "choice", "choices": ["a", "b"]})
    assert c.validate("a") == "a"
    with pytest.raises(ConfigValidationError):
        c.validate("z")
    b = ParameterDef.from_dict({"section": "s", "key": "k", "label": "K", "type": "bool"})
    assert b.validate("true") == "1"
    assert b.validate("0") == "0"


def test_pattern():
    p = ParameterDef.from_dict({"section": "s", "key": "country", "label": "C", "pattern": "[A-Z]{2}"})
    assert p.validate("US") == "US"
    with pytest.raises(ConfigValidationError):
        p.validate("usa")
