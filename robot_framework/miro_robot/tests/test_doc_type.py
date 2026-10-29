"""Doc. Type on the Details tab: selected by the DESCRIPTION the dashboard stored on the invoice."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "libraries"))

import pytest                                                   # noqa: E402

from miro.miro_screen import MiroScreen, pick_doc_type          # noqa: E402
from miro.sapgui import SapError                                 # noqa: E402

ENTRIES = [("AA", "Asset Posting"), ("SA", "G/L account document"), ("DR", "Customer invoice"),
           ("X1", "Invoice - R'gaon R1"), ("X4", "Invoice - AMBT1"), ("XD", "Invoice - INMT1"), ("XK", "Invoice - 2003")]


def test_pick_by_description_then_key():
    assert pick_doc_type(ENTRIES, "X1", "Invoice - R'gaon R1") == "X1"
    assert pick_doc_type(ENTRIES, "", "invoice - ambt1") == "X4"             # case-insensitive
    assert pick_doc_type(ENTRIES, "XD", "") == "XD"                          # key only
    assert pick_doc_type(ENTRIES, "ZZ", "Invoice - INMT1") == "XD"           # description wins over a wrong key
    assert pick_doc_type(ENTRIES, "X1", "Nonsense") == "X1"                   # key still found
    assert pick_doc_type(ENTRIES, "", "Invoice") == ""                        # not unique -> nothing guessed
    assert pick_doc_type(ENTRIES, "Q9", "Not there") == ""


class _Entries:
    def __init__(self, items):
        self.items = items
        self.Count = len(items)

    def ElementAt(self, i):
        k, v = self.items[i]
        return type("E", (), {"Key": k, "Value": v})()


class _Combo:
    Value = "Asset Posting"

    def __init__(self):
        self.Entries = _Entries(ENTRIES)


class _Sess:
    def __init__(self):
        self.combo, self.set = _Combo(), None

    def find_name(self, name, **kw):
        return self.combo

    def set_field(self, name, value, **kw):
        self.set = (name, value)

    def _show(self, *a, **k):
        pass


class _Cfg:
    @staticmethod
    def doc_type_for_plant(p):
        return ""


def _screen():
    sc = MiroScreen.__new__(MiroScreen)
    sc.s, sc.cfg, sc.notes = _Sess(), _Cfg(), []
    sc.note = lambda m: sc.notes.append(m)
    return sc


def test_set_doc_type_uses_invoice_description():
    sc = _screen()
    sc.set_doc_type({"plant_code": "2002", "doc_type": "X1", "doc_type_desc": "Invoice - R'gaon R1"})
    assert sc.s.set == ("INVFO-BLART", "X1")


def test_set_doc_type_error_when_not_in_dropdown():
    sc = _screen()
    with pytest.raises(SapError, match="not in the SAP drop-down"):
        sc.set_doc_type({"plant_code": "2999", "doc_type": "XZ", "doc_type_desc": "Invoice - Nowhere"})
    assert sc.s.set is None                                                  # nothing typed into SAP


def test_old_behaviour_without_invoice_doc_type():
    sc = _screen()
    sc.set_doc_type({"plant_code": "2002"})                                  # no doc_type, no mapping -> SAP default kept
    assert sc.s.set is None
