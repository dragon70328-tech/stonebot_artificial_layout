"""历史回填工具的单元测试。

用合成仓库（画像目录 + output 契约 + 伪源图）覆盖回填判定：缺口识别、
源图定位优先级、件数不一致拦截、指纹复核。读图探针是注入的，不碰真实图纸。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.case_backfill import (
    CaseInfo,
    RecognizedRun,
    apply_plan,
    build_plan,
    counts_from_panels,
    has_blocked_actions,
    index_sources,
    resolve_source,
    scan_recognized_runs,
)
from src.cli_audit import file_sha256


def _write_json(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def _profile(name: str, **extra) -> dict:
    data = {"name": name, "version": "1.0.0", "panel_layer": "0"}
    data.update(extra)
    return data


def _fake_dxf(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _recognized(profile_name: str, digest: str, panels: list[dict]) -> dict:
    return {
        "profile_name": profile_name,
        "input_digest": digest,
        "panels": panels,
    }


def _panel(holes: int = 0, number: str | None = None) -> dict:
    return {
        "holes": [{} for _ in range(holes)],
        "original_number": number,
    }


def _probe(counts: dict, fingerprint: dict | None = None):
    """注入的读图探针：固定件数与指纹，不读真实图纸。"""

    def probe(path, profile):  # noqa: ANN001, ARG001
        return {
            "counts": dict(counts),
            "fingerprint": dict(
                fingerprint
                if fingerprint is not None
                else {"layer_counts": {"0": 1}}
            ),
        }

    return probe


@pytest.fixture
def repo(tmp_path: Path) -> SimpleNamespace:
    profiles = tmp_path / "drawing_profiles"
    sources = tmp_path / "sources"
    output = tmp_path / "output"
    for path in (profiles, sources, output):
        path.mkdir()
    return SimpleNamespace(
        profiles=profiles, sources=sources, output=output, tmp=tmp_path
    )


def _plan(repo: SimpleNamespace, name: str, **kwargs):
    kwargs.setdefault("sources_dir", repo.sources)
    return build_plan(
        profiles_dir=repo.profiles,
        output_root=repo.output,
        only=[name],
        **kwargs,
    )[0]


def test_counts_from_panels_counts_holes_and_numbers():
    panels = [
        _panel(holes=2, number="1001-01"),
        _panel(),
        _panel(number="1001-02"),
    ]
    assert counts_from_panels(panels) == (3, 2, 2)


def test_scan_recognized_runs_groups_by_profile_and_skips_junk(repo):
    _write_json(
        repo.output / "20260101_010101_a" / "a_recognized.json",
        _recognized("a", "a" * 64, [_panel(number="1")]),
    )
    _write_json(
        repo.output / "20260101_020202_a" / "a_recognized.json",
        _recognized("a", "a" * 64, [_panel(number="1")]),
    )
    _write_json(
        repo.output / "20260101_030303_b" / "b_recognized.json",
        _recognized("b", "b" * 64, []),
    )
    _write_json(repo.output / "junk" / "junk_recognized.json", {"hello": 1})

    runs = scan_recognized_runs(repo.output)

    assert set(runs) == {"a", "b"}
    assert len(runs["a"]) == 2
    assert runs["a"][0].counts == (1, 0, 1)
    assert runs["b"][0].counts == (0, 0, 0)


def test_index_sources_maps_by_content(repo):
    first = _fake_dxf(repo.sources / "first.dxf", b"AAA")
    _fake_dxf(repo.sources / "notes.txt", b"AAA")

    index = index_sources(repo.sources)

    assert index == {file_sha256(first): first}


def test_resolve_source_prefers_case_over_run(repo):
    case_source = _fake_dxf(repo.sources / "case.dxf", b"case")
    run_source = _fake_dxf(repo.sources / "run.dxf", b"run")
    cases = [
        CaseInfo(
            path=Path("case.json"),
            data={"case_id": "a", "source_sha256": file_sha256(case_source)},
        )
    ]
    runs = [
        RecognizedRun(
            contract_path=Path("a_recognized.json"),
            profile_name="a",
            input_digest=file_sha256(run_source),
            counts=(1, 0, 1),
        )
    ]

    ref, findings = resolve_source("a", cases, runs, index_sources(repo.sources))

    assert ref is not None and ref.origin == "case"
    assert ref.path == case_source
    assert any("历史运行摘要与案例不一致" in item for item in findings)


def test_resolve_source_explicit_override_wins(repo):
    chosen = _fake_dxf(repo.sources / "chosen.dxf", b"chosen")

    ref, _ = resolve_source(
        "a", [], [], index_sources(repo.sources), {"a": str(chosen)}
    )

    assert ref is not None and ref.origin == "explicit"
    assert ref.sha256 == file_sha256(chosen)


def test_resolve_source_falls_back_to_recorded_path(repo):
    recorded = _fake_dxf(repo.sources / "moved.dxf", b"moved")
    cases = [
        CaseInfo(
            path=Path("moved.json"),
            data={
                "case_id": "moved",
                "fingerprint": {"path": str(repo.tmp / "gone" / "moved.dxf")},
            },
        )
    ]

    ref, findings = resolve_source(
        "moved", cases, [], index_sources(repo.sources), None, repo.sources
    )

    assert ref is not None and ref.origin == "case-path"
    assert ref.path == recorded
    assert any("记录的原图路径回找" in item for item in findings)


def test_build_plan_fills_missing_case_counts_and_fingerprint(repo):
    source = _fake_dxf(repo.sources / "alpha.dxf", b"alpha")
    _write_json(repo.profiles / "alpha.json", _profile("alpha"))
    _write_json(
        repo.output / "20260101_010101_alpha" / "alpha_recognized.json",
        _recognized("alpha", file_sha256(source), [_panel(number="1")]),
    )

    report = _plan(
        repo,
        "alpha",
        probe=_probe({"panels": 1, "holes": 0, "numbers": 1}),
    )

    assert [action.kind for action in report.actions] == [
        "profile:fill_expected_counts",
        "profile:fill_fingerprint",
        "case:create",
    ]
    assert not has_blocked_actions(report)
    assert report.source is not None and report.source.origin == "recognized"


def test_build_plan_skips_complete_profile_without_reading(repo):
    source = _fake_dxf(repo.sources / "done.dxf", b"done")
    counts = {"panels": 2, "holes": 0, "numbers": 2}
    _write_json(
        repo.profiles / "done.json",
        _profile(
            "done",
            fingerprint={"layer_counts": {"0": 5}},
            expected_counts=counts,
        ),
    )
    _write_json(
        repo.profiles / "validated_cases" / "done.json",
        {
            "case_id": "done",
            "profile_name": "done",
            "source_sha256": file_sha256(source),
            "expected_counts": counts,
        },
    )

    def boom(path, profile):  # noqa: ANN001, ARG001
        raise AssertionError("五件齐的画像不应重读图纸")

    report = _plan(repo, "done", probe=boom)

    assert report.complete
    assert report.findings == ["五件齐，无需回填"]


def test_build_plan_blocks_missing_case_when_counts_mismatch(repo):
    source = _fake_dxf(repo.sources / "beta.dxf", b"beta")
    _write_json(
        repo.profiles / "beta.json",
        _profile(
            "beta",
            fingerprint={"layer_counts": {"0": 5}},
            expected_counts={"panels": 9, "holes": 0, "numbers": 9},
        ),
    )
    _write_json(
        repo.output / "20260101_010101_beta" / "beta_recognized.json",
        _recognized("beta", file_sha256(source), [_panel()]),
    )

    report = _plan(
        repo,
        "beta",
        probe=_probe({"panels": 1, "holes": 0, "numbers": 0}),
    )

    assert [action.kind for action in report.actions] == ["case:create"]
    assert report.actions[0].blocked_reason is not None
    assert has_blocked_actions(report)
    assert any("已声明件数与复算不一致" in item for item in report.findings)

    applied = apply_plan([report], write=True)
    assert not (repo.profiles / "validated_cases" / "beta.json").exists()
    assert applied[0].startswith("[跳过]")


def test_build_plan_keeps_identity_fill_when_counts_mismatch(repo):
    """件数对不上时只拦"带件数"的动作，补指纹/补 sha256 照做。"""
    source = _fake_dxf(repo.sources / "gamma.dxf", b"gamma")
    digest = file_sha256(source)
    _write_json(
        repo.profiles / "gamma.json",
        _profile(
            "gamma",
            expected_counts={"panels": 9, "holes": 0, "numbers": 9},
        ),
    )
    _write_json(
        repo.profiles / "validated_cases" / "gamma.json",
        {"case_id": "gamma", "profile_name": "gamma"},
    )
    _write_json(
        repo.output / "20260101_010101_gamma" / "gamma_recognized.json",
        _recognized("gamma", digest, [_panel()]),
    )

    report = _plan(
        repo,
        "gamma",
        probe=_probe({"panels": 1, "holes": 0, "numbers": 0}),
    )

    assert {action.kind for action in report.actions} == {
        "profile:fill_fingerprint",
        "case:fill_sha256",
    }
    assert not has_blocked_actions(report)

    apply_plan([report], write=True)
    case = json.loads(
        (repo.profiles / "validated_cases" / "gamma.json").read_text(
            encoding="utf-8"
        )
    )
    assert case["source_sha256"] == digest
    profile = json.loads(
        (repo.profiles / "gamma.json").read_text(encoding="utf-8")
    )
    assert profile["fingerprint"]
    assert profile["expected_counts"] == {
        "panels": 9,
        "holes": 0,
        "numbers": 9,
    }


def test_build_plan_requires_sources_dir(repo):
    _write_json(repo.profiles / "delta.json", _profile("delta"))

    report = build_plan(
        profiles_dir=repo.profiles, output_root=repo.output, only=["delta"]
    )[0]

    assert report.blockers == ["未提供源图目录（--sources），无法核对件数"]
    assert report.actions == []


def test_apply_plan_dry_run_writes_nothing(repo):
    source = _fake_dxf(repo.sources / "eps.dxf", b"eps")
    _write_json(repo.profiles / "eps.json", _profile("eps"))
    _write_json(
        repo.output / "20260101_010101_eps" / "eps_recognized.json",
        _recognized("eps", file_sha256(source), [_panel(number="7")]),
    )

    report = _plan(
        repo, "eps", probe=_probe({"panels": 1, "holes": 0, "numbers": 1})
    )
    applied = apply_plan([report], write=False)

    assert all(line.startswith("[待回填]") for line in applied)
    assert not (repo.profiles / "validated_cases" / "eps.json").exists()
    assert "fingerprint" not in json.loads(
        (repo.profiles / "eps.json").read_text(encoding="utf-8")
    )


def test_fingerprint_conflict_blocks_identity_fill(repo):
    source = _fake_dxf(repo.sources / "zeta.dxf", b"zeta")
    _write_json(repo.profiles / "zeta.json", _profile("zeta"))
    _write_json(
        repo.profiles / "validated_cases" / "zeta.json",
        {
            "case_id": "zeta",
            "profile_name": "zeta",
            "source_sha256": file_sha256(source),
            "fingerprint": {"layer_counts": {"0": 99}},
        },
    )

    report = _plan(
        repo,
        "zeta",
        probe=_probe(
            {"panels": 1, "holes": 0, "numbers": 1},
            fingerprint={"layer_counts": {"0": 1}},
        ),
    )

    assert has_blocked_actions(report)
    assert any("结构指纹与本次复算不一致" in item for item in report.findings)

    apply_plan([report], write=True)
    profile = json.loads(
        (repo.profiles / "zeta.json").read_text(encoding="utf-8")
    )
    assert "fingerprint" not in profile


def test_name_based_binding_rejected_when_fingerprint_differs(repo):
    _fake_dxf(repo.sources / "moved.dxf", b"moved")
    _write_json(repo.profiles / "moved.json", _profile("moved"))
    _write_json(
        repo.profiles / "validated_cases" / "moved.json",
        {
            "case_id": "moved",
            "profile_name": "moved",
            "fingerprint": {
                "path": str(repo.tmp / "gone" / "moved.dxf"),
                "layer_counts": {"0": 99},
            },
        },
    )

    report = _plan(
        repo,
        "moved",
        probe=_probe(
            {"panels": 1, "holes": 0, "numbers": 1},
            fingerprint={"layer_counts": {"0": 1}},
        ),
    )

    assert report.source is None
    assert report.actions == []
    assert any("绑定已放弃" in item for item in report.blockers)


def test_name_based_binding_accepted_when_fingerprint_matches(repo):
    source = _fake_dxf(repo.sources / "match.dxf", b"match")
    _write_json(
        repo.profiles / "match.json",
        _profile(
            "match",
            expected_counts={"panels": 1, "holes": 0, "numbers": 1},
        ),
    )
    _write_json(
        repo.profiles / "validated_cases" / "match.json",
        {
            "case_id": "match",
            "profile_name": "match",
            "fingerprint": {
                "path": str(repo.tmp / "gone" / "match.dxf"),
                "layer_counts": {"0": 4},
            },
            "expected_counts": {"panels": 1, "holes": 0, "numbers": 1},
        },
    )

    report = _plan(
        repo,
        "match",
        probe=_probe(
            {"panels": 1, "holes": 0, "numbers": 1},
            fingerprint={"layer_counts": {"0": 4}},
        ),
    )

    assert report.source is not None and report.source.path == source
    assert report.source.origin == "case-path"
    assert {action.kind for action in report.actions} == {
        "profile:fill_fingerprint",
        "case:fill_sha256",
    }
