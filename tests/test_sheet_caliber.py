"""大板口径：标称 / 让尺 / 可利用尺寸 三层（2026-10-13 用户确认）。

大板尺寸 = 标称；让尺 = 标称之外的实物余量，每维度总量加一次，只为消化刀缝；
可利用尺寸 = 零件实际能占的范围，默认等于标称。
"""

import main as app
from src.nesting_contracts import SheetSpec


def test_usable_excludes_oversize_by_default():
    caliber = app.resolve_sheet_caliber(3200, 1600, sheet_oversize=(30, 30))

    assert (caliber.usable_w_mm, caliber.usable_h_mm) == (3200, 1600)
    # 每维度总量 +30（实物 3230x1630），不是每边 +30（3260x1660）
    assert (caliber.physical_w_mm, caliber.physical_h_mm) == (3230, 1630)
    assert (caliber.allowance_w_mm, caliber.allowance_h_mm) == (30, 30)
    assert caliber.usable_includes_oversize is False


def test_usable_includes_oversize_when_declared():
    caliber = app.resolve_sheet_caliber(
        3200,
        1600,
        sheet_oversize=(30, 30),
        usable_includes_oversize=True,
    )

    assert (caliber.usable_w_mm, caliber.usable_h_mm) == (3230, 1630)
    # 让尺已经进了可利用区，实物不再有额外余量留给刀缝
    assert (caliber.allowance_w_mm, caliber.allowance_h_mm) == (0, 0)
    assert caliber.usable_includes_oversize is True


def test_no_oversize_means_no_allowance():
    caliber = app.resolve_sheet_caliber(3200, 1600)

    assert (caliber.usable_w_mm, caliber.usable_h_mm) == (3200, 1600)
    assert (caliber.oversize_w_mm, caliber.oversize_h_mm) == (0, 0)
    assert (caliber.allowance_w_mm, caliber.allowance_h_mm) == (0, 0)


def test_kerf_reserve_is_alias_of_sheet_oversize():
    alias = app.resolve_sheet_caliber(3200, 1600, kerf_reserve_mm=30)

    assert (alias.oversize_w_mm, alias.oversize_h_mm) == (30, 30)
    assert (alias.usable_w_mm, alias.usable_h_mm) == (3200, 1600)
    assert (alias.allowance_w_mm, alias.allowance_h_mm) == (30, 30)


def test_both_oversize_flags_do_not_double_count():
    """回归：让尺曾被计两次（排板空间 +30、校验再 +30 = 60）。"""
    caliber = app.resolve_sheet_caliber(
        3200,
        1600,
        sheet_oversize=(30, 30),
        kerf_reserve_mm=30,
    )

    assert (caliber.oversize_w_mm, caliber.oversize_h_mm) == (30, 30)
    assert (caliber.allowance_w_mm, caliber.allowance_h_mm) == (30, 30)
    assert caliber.physical_w_mm == 3230


def test_cli_caliber_agrees_with_api_sheet_spec():
    """同一句口径说明，CLI 与 API 契约（SheetSpec）必须得出同样数字。

    CLI: 大板 3200x1600、让尺各 30（每维度总量）、可利用尺寸不含让尺
    API: nominal 3200x1600、usable 3200x1600、oversize total 30
    """
    caliber = app.resolve_sheet_caliber(3200, 1600, sheet_oversize=(30, 30))
    spec = SheetSpec(
        nominal_width_mm=3200,
        nominal_height_mm=1600,
        usable_width_mm=3200,
        usable_height_mm=1600,
        oversize_enabled=True,
        oversize_mode="total",
        oversize_width_mm=30,
        oversize_height_mm=30,
    )

    assert spec.usable_width_mm == caliber.usable_w_mm
    # 契约里"实物尺寸"叫 effective_*（= 可利用 + 让尺）
    assert spec.effective_width_mm == caliber.physical_w_mm == 3230
    assert spec.effective_height_mm == caliber.physical_h_mm == 1630
    assert spec.oversize_added_width_mm == caliber.allowance_w_mm == 30


def test_cli_caliber_agrees_with_api_when_usable_includes_oversize():
    """声明"可利用尺寸包括让尺"时，契约里把 usable 直接写成 3230（无额外让尺）。"""
    caliber = app.resolve_sheet_caliber(
        3200,
        1600,
        sheet_oversize=(30, 30),
        usable_includes_oversize=True,
    )
    spec = SheetSpec(
        nominal_width_mm=3200,
        nominal_height_mm=1600,
        usable_width_mm=3230,
        usable_height_mm=1630,
    )

    assert spec.usable_width_mm == caliber.usable_w_mm == 3230
    assert spec.effective_width_mm == caliber.physical_w_mm == 3230
    assert spec.oversize_added_width_mm == caliber.allowance_w_mm == 0
