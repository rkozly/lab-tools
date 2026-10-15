import pytest

from devreader.config.uci import UciDocument

SAMPLE = """\
# Global comment
config qcawifi 'qcawifi'
        option rf_freq1_KHz '26000000'
        option rf_freq2_KHz '26000000'   # trailing comment
\toption other "x y"

config wifi-device 'wifi0'
\toption channel 36
\tlist maclist 'aa'
\tlist maclist 'bb'

config wifi-iface
\toption device 'wifi0'
"""


def test_parse_sections_and_options():
    doc = UciDocument(SAMPLE)
    assert [s.type for s in doc.sections] == ["qcawifi", "wifi-device", "wifi-iface"]
    assert doc.get("qcawifi", "rf_freq1_KHz") == "26000000"
    assert doc.get("qcawifi", "rf_freq2_KHz") == "26000000"
    assert doc.get("qcawifi", "other") == "x y"
    assert doc.get("wifi0", "channel") == "36"
    assert [o.value for o in doc.find_section("wifi0").lists["maclist"]] == ["aa", "bb"]
    assert doc.find_section("@wifi-iface[0]").name is None
    assert doc.find_section("@wifi-iface[-1]").options["device"].value == "wifi0"


def test_roundtrip_is_exact():
    for text in (SAMPLE, SAMPLE.replace("\n", "\r\n"), SAMPLE.rstrip("\n")):
        assert UciDocument(text).to_text() == text


def test_set_changes_only_value():
    doc = UciDocument(SAMPLE)
    assert doc.set("qcawifi", "rf_freq2_KHz", "27000000")
    assert doc.to_text() == SAMPLE.replace(
        "option rf_freq2_KHz '26000000'   # trailing comment",
        "option rf_freq2_KHz '27000000'   # trailing comment",
    )
    assert doc.changed_lines == {3}


def test_set_same_value_is_noop():
    doc = UciDocument(SAMPLE)
    assert not doc.set("qcawifi", "rf_freq1_KHz", "26000000")
    assert doc.to_text() == SAMPLE


def test_set_bare_and_double_quoted():
    doc = UciDocument(SAMPLE)
    doc.set("wifi0", "channel", "149")
    doc.set("qcawifi", "other", 'a "b"')
    text = doc.to_text()
    assert "\toption channel 149\n" in text
    assert '\toption other "a \\"b\\""\n' in text
    assert UciDocument(text).get("wifi0", "channel") == "149"


def test_bare_value_needing_quotes():
    doc = UciDocument(SAMPLE)
    doc.set("wifi0", "channel", "a b")
    assert "\toption channel 'a b'\n" in doc.to_text()


def test_insert_missing_option():
    doc = UciDocument(SAMPLE)
    doc.set("qcawifi", "rf_bw", "20")
    lines = doc.to_text().splitlines()
    assert lines[5] == "\toption rf_bw '20'" or lines[5] == "        option rf_bw '20'"
    assert doc.get("qcawifi", "rf_bw") == "20"


def test_insert_at_eof_without_newline():
    doc = UciDocument("config qcawifi 'qcawifi'\n\toption a '1'")
    doc.set("qcawifi", "b", "2")
    assert doc.to_text() == "config qcawifi 'qcawifi'\n\toption a '1'\n\toption b '2'"


def test_set_without_create_raises():
    doc = UciDocument(SAMPLE)
    with pytest.raises(KeyError):
        doc.set("qcawifi", "missing", "1", create=False)
    with pytest.raises(KeyError):
        doc.set("nosuchsection", "k", "1")


def test_rejects_unsafe_values():
    doc = UciDocument(SAMPLE)
    with pytest.raises(ValueError):
        doc.set("qcawifi", "rf_freq1_KHz", "1'2")
    with pytest.raises(ValueError):
        doc.set("qcawifi", "rf_freq1_KHz", "1\n2")


def test_section_type_restriction():
    doc = UciDocument("config wifi-device 'qcawifi'\n\toption x '1'\nconfig qcawifi 'qcawifi'\n\toption x '2'\n")
    assert doc.get("qcawifi", "x") == "1"
    assert doc.get("qcawifi", "x", section_type="qcawifi") == "2"


def test_non_utf8_bytes_roundtrip():
    raw = b"config qcawifi 'qcawifi'\n\toption name '\xff\xfe'\n\toption rf_freq1_KHz '1'\n"
    doc = UciDocument.from_bytes(raw)
    doc.set("qcawifi", "rf_freq1_KHz", "2")
    assert doc.to_bytes() == raw.replace(b"'1'", b"'2'")
