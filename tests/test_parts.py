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
    r = parts.search("10k", package="0402", client=client())
    assert r["matched"] == 2
    assert r["parts"][0].startswith("C2 [B] BASIC | 0402")
    assert "$0.002" in r["parts"][0] and "ROHS" not in r["parts"][0]
    assert r["parts"][1].startswith("C1 [E]")


def test_basic_only():
    r = parts.search("10k", package="0402", basic_only=True, client=client())
    assert [p.split()[0] for p in r["parts"]] == ["C2"]


def test_http_error_becomes_tool_error():
    with pytest.raises(ToolError):
        parts.search("10k", client=client({}, status=503))
