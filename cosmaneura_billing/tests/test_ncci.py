"""NCCI loader: ingests the CMS PTP/MUE layouts and answers edit queries."""
import pytest

from cosmaneura_billing.knowledge.ncci import NCCIEdits


@pytest.fixture()
def ncci(tmp_path):
    ptp = tmp_path / "ptp.txt"
    ptp.write_text(
        "Column 1\tColumn 2\tEffective Date\tModifier\tRationale\n"
        "99213\t36415\t20240101\t1\tStandards of medical practice\n"
        "45378\t45380\t20240101\t0\tMisuse of column two code\n"
    )
    mue = tmp_path / "mue.txt"
    mue.write_text(
        "HCPCS/CPT Code\tPractitioner Services MUE Values\tMAI\n"
        "96372\t4\t3\n11042\t1\t2\n"
    )
    return NCCIEdits.from_files(ptp_path=str(ptp), mue_path=str(mue))


def test_ptp_modifier_may_bypass(ncci):
    hit = ncci.ptp_check("99213", "36415")
    assert hit is not None and hit.modifier_indicator == "1"


def test_ptp_no_modifier_allowed_and_order_insensitive(ncci):
    assert ncci.ptp_check("45378", "45380").modifier_indicator == "0"
    assert ncci.ptp_check("45380", "45378").modifier_indicator == "0"


def test_ptp_miss_returns_none(ncci):
    assert ncci.ptp_check("99213", "99214") is None


def test_mue_limits(ncci):
    assert ncci.mue_limit("96372") == 4
    assert ncci.mue_limit("11042") == 1
    assert ncci.mue_limit("99999") is None


@pytest.mark.parametrize('suffix', ['.csv', '.tsv', '.tsv.gz'])
def test_streamed_titles_duplicates_and_directed_precedence(tmp_path, suffix):
    import gzip

    path = tmp_path / ('ptp' + suffix)
    delim = ',' if suffix == '.csv' else '\t'
    rows = [
        ['CMS quarterly practitioner edits'],
        ['Column 1', 'Column 2', 'Modifier'],
        [' 99.213 ', ' 36415 ', '1'],
        ['99213', '36415', '0'],  # Duplicate counts, last modifier wins.
        ['36415', '99213', '1'],  # Forward wins when both orientations exist.
        ['A1234', 'B1234', ''],
        ['C1234', 'D1234', 'x-other'],
        ['', 'D1234', '0'],
        ['short'],
    ]
    text = '\n'.join(delim.join(row) for row in rows) + '\n'
    if suffix.endswith('.gz'):
        with gzip.open(path, 'wt') as stream:
            stream.write(text)
    else:
        path.write_text(text)
    edits = NCCIEdits()
    assert edits.load_ptp(path) == 5
    assert edits.stats == '4 PTP pairs, 0 MUE limits'
    assert edits.ptp_check('99.213', '36415').modifier_indicator == '0'
    assert edits.ptp_check('36415', '99213').column_one == '36415'
    assert edits.ptp_check('36415', '99213').modifier_indicator == '1'
    deleted = edits.ptp_check('b1234', 'a1234')
    assert (deleted.column_one, deleted.column_two, deleted.meaning) == ('A1234', 'B1234', 'not_applicable')
    assert edits.ptp_check('C1234', 'D1234').meaning == 'unknown'
    # Query values cannot mutate the retained catalog's next result.
    edits.ptp_check('99213', '36415').modifier_indicator = '9'
    assert edits.ptp_check('99213', '36415').modifier_indicator == '0'


def test_text_header_fallback_and_late_header(tmp_path):
    path = tmp_path / 'ptp.txt'
    path.write_text('first\tsecond\tthird\n99213\t36415\t0\n45378\t45380\t1\n')
    edits = NCCIEdits()
    assert edits.load_ptp(path) == 2
    # Without recognizable labels, first/second columns and missing modifier9.
    assert edits.ptp_check('99213', '36415').modifier_indicator == '9'
    path.write_text('first\tsecond\n11111\t22222\nColumn 1\tColumn 2\tModifier\n99213\t36415\t1\n')
    assert edits.load_ptp(path) == 1
    assert edits.ptp_check('11111', '22222') is None
    assert edits.ptp_check('99213', '36415').modifier_indicator == '1'
    empty = tmp_path / 'empty.txt'
    empty.write_text('')
    assert edits.load_ptp(empty) == 0


def test_mue_duplicates_invalid_missing_and_empty_codes(tmp_path):
    path = tmp_path / 'mue.csv'
    path.write_text('Title\nHCPCS/CPT Code,Practitioner MUE Value\n a.1234 ,3.9\nA1234,2\nA1234,invalid\n,4\nB1234\n')
    edits = NCCIEdits()
    assert edits.load_mue(path) == 3
    assert edits.mue_limit('a.1234') == 2
    assert edits.mue_limit('') == 4  # Preserve current empty-code semantics.
    assert edits.mue_limit('B1234') is None
    assert edits.stats == '0 PTP pairs, 2 MUE limits'


def test_text_stream_is_consumed_and_closed_inside_load(tmp_path, monkeypatch):
    import io
    from cosmaneura_billing.knowledge import ncci as module

    stream = io.StringIO('Column 1\tColumn 2\tModifier\n99213\t36415\t1\n')
    monkeypatch.setattr(module, '_open', lambda unused: stream)
    assert NCCIEdits().load_ptp(tmp_path / 'fake.txt') == 1
    assert stream.closed


def test_xlsx_first_row_and_workbook_close_even_on_iteration_error(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    class Workbook:
        closed = False
        fail = False

        @property
        def active(self):
            return self

        def iter_rows(self, *, values_only):
            assert values_only
            yield ('Column 1', 'Column 2', 'Modifier')
            yield ('99213', '36415', 1)
            if self.fail:
                raise RuntimeError('authored workbook failure')
            yield (None, '36415', 0)

        def close(self):
            self.closed = True

    workbook = Workbook()

    def load(path, *, read_only, data_only):
        assert read_only and data_only
        return workbook

    monkeypatch.setitem(sys.modules, 'openpyxl', SimpleNamespace(load_workbook=load))
    assert NCCIEdits().load_ptp(tmp_path / 'edits.xlsx') == 1
    assert workbook.closed
    workbook.closed, workbook.fail = False, True
    with pytest.raises(RuntimeError, match='authored workbook failure'):
        NCCIEdits().load_ptp(tmp_path / 'edits.xlsm')
    assert workbook.closed


def test_xlsx_does_not_scan_titles_like_text(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    class Workbook:
        active = SimpleNamespace(iter_rows=lambda **unused: iter([
            ('Title', None), ('Column 1', 'Column 2'), ('99213', '36415')]))

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, 'openpyxl', SimpleNamespace(load_workbook=lambda *a, **k: Workbook()))
    edits = NCCIEdits()
    assert edits.load_ptp(tmp_path / 'edits.xlsx') == 2
    assert edits.ptp_check('Column 1', 'Column 2').modifier_indicator == '9'
