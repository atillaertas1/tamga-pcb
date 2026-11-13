from pcbmcp.report import summarize


def _v(typ, sev, *descs):
    return {"type": typ, "severity": sev, "description": typ.replace("_", " "),
            "items": [{"description": d, "pos": {"x": 1.0, "y": 2.0}} for d in descs]}


def test_clean_report():
    assert summarize({"violations": [], "unconnected_items": []})["verdict"] == "CLEAN"


def test_groups_by_type_errors_first_and_collects_nets():
    rep = {
        "violations": [
            _v("silk_overlap", "warning", "Text R1"),
            _v("clearance", "error", "Track [GND] on F.Cu", "Pad 1 [VIN] of C1"),
            _v("clearance", "error", "Track [GND] on F.Cu", "Pad 2 [+3V3] of C2"),
        ],
        "unconnected_items": [_v("unconnected_items", "error", "Pad 1 [VIN] of C1", "Pad 3 [VIN] of U1")],
    }
    s = summarize(rep, max_examples=1)
    assert s["total"] == 4 and s["errors"] == 3 and s["warnings"] == 1
    first = s["types"][0]
    assert first["type"] == "clearance" and first["count"] == 2
    assert first["nets"] == ["+3V3", "GND", "VIN"]
    assert len(first["examples"]) == 1 and "@(1.0,2.0)" in first["examples"][0]
    assert s["types"][-1]["severity"] == "warning"


def test_erc_nested_sheet_format():
    rep = {"sheets": [{"path": "/", "violations": [_v("pin_not_connected", "error", "Pin 3 [NC] of U1")]}]}
    s = summarize(rep)
    assert s["total"] == 1 and s["types"][0]["type"] == "pin_not_connected"


def test_type_cap():
    rep = {"violations": [_v(f"t{i}", "warning", "x") for i in range(20)]}
    s = summarize(rep, max_types=5)
    assert len(s["types"]) == 5 and s["more_types"] == 15
