import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tamga_pcb import parts

FAKE = {"components": [
    {"lcsc": 1, "mfr": "EXT", "package": "0402", "stock": 9_000_000, "price": 0.001,
     "description": "10k ext ROHS", "is_basic": False, "is_preferred": False},
    {"lcsc": 2, "mfr": "BASIC", "package": "0402", "stock": 5000, "price": "1-49:0.002,50-:0.001",
     "description": "10k basic ROHS", "is_basic": True, "is_preferred": False},
    {"lcsc": 3, "mfr": "WRONGPKG", "package": "0603", "stock": 5000, "price": 0.001,
     "description": "10k", "is_basic": True, "is_preferred": False},
    {"lcsc": 4, "mfr": "NOSTOCK", "package": "0402", "stock": 3, "price": 0.001,
     "description": "10k", "is_basic": True, "is_preferred": False},
]}


def client(payload=FAKE, status=200):
    return httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(status, json=payload)))


def test_filters_and_ranks_basic_first():
    r = parts.search("RC0402", package="0402", client=client())
    assert r["matched"] == 2
    assert r["parts"][0].startswith("C2 [B] BASIC | 0402")
    assert "$0.002" in r["parts"][0] and "ROHS" not in r["parts"][0]
    assert r["parts"][1].startswith("C1 [E]")


def test_basic_only():
    r = parts.search("RC0402", package="0402", basic_only=True, client=client())
    assert [p.split()[0] for p in r["parts"]] == ["C2"]


def test_http_error_becomes_tool_error():
    with pytest.raises(ToolError):
        parts.search("10k", client=client({}, status=503))


def test_rejected_query_passes_api_hint_through():
    """jlcsearch tek terimli sorguyu 400 ile reddeder; 'unavailable' değil, API'nin önerisi dönmeli."""
    hint = {"error": "Invalid search", "message": 'Add another search term, such as "8MHz crystal"'}
    with pytest.raises(ToolError, match=r"rejected.*8MHz crystal.*resistor 5\.1k"):
        parts.search("5.1k", client=client(hint, status=400))


@pytest.mark.parametrize("q,want", [("5.1k", ("resistors", 5100)), ("resistor 10k", ("resistors", 10000)),
                                    ("4k7", ("resistors", 4700)), ("100R", ("resistors", 100)),
                                    ("1M", ("resistors", 1e6)), ("100nF", ("capacitors", 1e-7)),
                                    ("capacitor 10uF", ("capacitors", 1e-5)), ("22pF 50V", ("capacitors", 22e-12)),
                                    ("CH340C", None), ("LED red", None), ("USB-C 16P", None)])
def test_value_queries_go_to_value_endpoints(q, want):
    got = parts.value_query(q)
    assert (got is None) if want is None else (got[0] == want[0] and abs(got[1] - want[1]) < want[1] * 1e-9)


def test_basic_part_with_unknown_stock_is_kept():
    """jlcsearch basic parçalarda stock=None döndürüyor; min_stock filtresi onları elememeli."""
    payload = {"resistors": [{"lcsc": 23186, "mfr": "0603WAF5101T5E", "package": "0603", "stock": None,
                              "price1": 0.0005, "description": "5.1kΩ", "is_basic": True},
                             {"lcsc": 9, "mfr": "LOW", "package": "0603", "stock": 3, "is_basic": False}]}
    seen = {}

    def handler(req):
        seen["path"] = req.url.path
        return httpx.Response(200, json=payload)
    r = parts.search("5.1k", package="0603", basic_only=True, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert seen["path"] == "/resistors/list.json"
    assert r["parts"] and r["parts"][0].startswith("C23186 [B]") and "stock ?" in r["parts"][0]


def test_bom_cost_sums_flags_extended_and_unknown():
    catalogue = {
        "C23186": {"lcsc": 23186, "price": "1-:0.002", "is_basic": True, "stock": None},
        "C84681": {"lcsc": 84681, "price": "1-:0.50", "is_basic": False, "is_preferred": False, "stock": 4},
    }

    def handler(req):
        code = req.url.params["search"]
        return httpx.Response(200, json={"components": [catalogue[code]] if code in catalogue else []})
    rows = [["5.1k", "R1,R2", "R_0603", "C23186"], ["CH340C", "U1", "SOIC-16", "C84681"],
            ["?", "U2", "X", "C999999"], ["HDR", "J2", "PinHeader", ""]]
    r = parts.bom_cost(rows, boards=5, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert r["parts_usd_per_board"] == 0.504
    assert r["extended_parts"] == ["C84681"] and r["extended_setup_usd"] == 3.0
    assert r["not_in_catalogue"] == ["C999999"] and r["low_stock"] == ["C84681 (stock 4)"]
