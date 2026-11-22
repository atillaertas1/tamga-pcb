"""IPC-2141 mikroşerit hesabı (KiCad gerekmez)."""
import pytest

from tamga_pcb import impedance


def test_four_layer_50_ohm_is_a_normal_track():
    r = impedance.track_for(50, 4)
    assert 0.30 <= r["track_mm"] <= 0.40 and abs(r["impedance_ohm"] - 50) < 0.5 and "warning" not in r


def test_usb_pair_on_four_layers():
    r = impedance.track_for(90, 4, diff_gap_mm=0.15)
    assert 0.2 <= r["track_mm"] <= 0.3 and r["gap_mm"] == 0.15


def test_two_layers_warn_that_controlled_impedance_is_impractical():
    r = impedance.track_for(90, 2, diff_gap_mm=0.15)
    assert r["track_mm"] > 1.0 and "4 layers" in r["warning"]


def test_six_layers_need_a_stackup():
    with pytest.raises(ValueError, match="stackup"):
        impedance.track_for(50, 6)
    assert impedance.track_for(50, 6, h_mm=0.1, er=4.2)["track_mm"] < 0.25
