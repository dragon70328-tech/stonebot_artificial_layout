"""大板口径：标称 / 让尺 / 可利用尺寸 三层（2026-10-13 用户确认）。

大板尺寸 = 标称；让尺 = 标称之外的实物余量，每维度总量加一次，只为消化刀缝；
可利用尺寸 = 零件实际能占的范围，默认等于标称。
"""

import main as app


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
