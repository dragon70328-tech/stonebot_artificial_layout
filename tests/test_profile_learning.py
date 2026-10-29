from __future__ import annotations

import json
import re
from pathlib import Path

from shapely.geometry import box

from src.drawing_profile import DrawingProfile, analyze_drawing
from src.job_context import JobContext
from src.models import Part
from src.profile_learning import (
    accept_profile_patch,
    list_profile_patches,
    matching_profile_override,
    propose_profile_patches,
    reject_profile_patch,
    review_profile_patch,
    rollback_profile_override,
)


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "dxf"
    / "雪花白洗手台（小利）1.8.dxf"
)
BASE_PROFILE = Path(__file__).parents[1] / "drawing_profiles" / "snow_white_basin.json"


def test_correction_proposes_only_missing_profile_evidence(tmp_path):
    source = tmp_path / "drawing.dxf"
    source.write_text("fake-dxf", encoding="utf-8")
    profile = DrawingProfile(
        name="demo",
        version="1.0.0",
        panel_layers=["A"],
        panel_layer="A",
        number_layers=["N"],
        label_pattern=r"^X-\d+$",
    )
    parts = [
        Part(
            id=1,
            number="A10",
            polygon=box(0, 0, 10, 10),
            outer_polygon=box(0, 0, 10, 10),
        ),
        Part(
            id=2,
            number="A20",
            polygon=box(20, 0, 30, 10),
            outer_polygon=box(20, 0, 30, 10),
        ),
    ]
    context = JobContext(
        dxf_path=str(source),
        job_id="job-1",
        project_id="project-1",
        drawing_profile=profile,
        correction_revision=1,
        parts=parts,
        parts_data=[
            {
                "index": 1,
                "original_number": "A10",
                "number_layer": "NUM",
                "layer": "B",
            },
            {
                "index": 2,
                "original_number": "A20",
                "number_layer": "NUM",
                "layer": "B",
            },
        ],
    )

    patches = propose_profile_patches(
        context,
        data_root=tmp_path,
        tenant_id="default",
        project_id="project-1",
    )

    assert len(patches) == 1
    changes = {
        item["target"]: item
        for item in patches[0]["changes"]
    }
    assert changes["panel_layers"]["after"] == ["A", "B"]
    assert changes["number_layers"]["after"] == ["N", "NUM"]
    pattern = changes["label_pattern"]["after"]
    assert re.match(pattern, "A10")
    assert re.match(pattern, "A20")
    assert patches[0]["status"] == "candidate"


def test_accept_patch_creates_project_override(tmp_path):
    profiles_root = tmp_path / "profiles"
    profiles_root.mkdir()
    base = json.loads(BASE_PROFILE.read_text(encoding="utf-8"))
    base["name"] = "test_base"
    (profiles_root / "test_base.json").write_text(
        json.dumps(base, ensure_ascii=False),
        encoding="utf-8",
    )
    patch = {
        "schema_version": "0.1.0",
        "patch_id": "profilepatch-test",
        "status": "candidate",
        "tenant_id": "default",
        "project_id": "project-1",
        "base_profile_name": "test_base",
        "changes": [
            {
                "target": "small_area_threshold",
                "before": base.get("small_area_threshold", 0),
                "after": 0.1,
                "evidence_count": 6,
            }
        ],
        "evidence": {
            "job_id": "job-1",
            "target_counts": {
                "panels": 6,
                "holes": 4,
                "numbers": 6,
            },
        },
        "validation": None,
    }
    patch_path = (
        tmp_path
        / "profile_learning"
        / "default"
        / "project-1"
        / "patches"
        / "profilepatch-test.json"
    )
    patch_path.parent.mkdir(parents=True)
    patch_path.write_text(
        json.dumps(patch, ensure_ascii=False),
        encoding="utf-8",
    )

    reviewed = review_profile_patch(
        tmp_path,
        patch_id="profilepatch-test",
        source_path=FIXTURE,
        profiles_root=profiles_root,
    )
    assert reviewed["status"] == "validated"
    assert Path(reviewed["validation"]["preview_path"]).exists()
    assert reviewed["validation"]["historical_replay"]["passed"] is True

    accepted = accept_profile_patch(
        tmp_path,
        patch_id="profilepatch-test",
        source_path=FIXTURE,
        profiles_root=profiles_root,
    )

    assert accepted["status"] == "active"
    assert accepted["validation"]["accepted"] is True
    override_root = (
        tmp_path
        / "tenants"
        / "default"
        / "project-1"
        / "profile_overrides"
    )
    matched = matching_profile_override(FIXTURE, override_root)
    assert matched is not None
    profile, meta = matched
    assert profile["name"] == "test_base"
    assert profile["small_area_threshold"] == 0.1
    assert meta["patch_id"] == "profilepatch-test"


def test_reject_patch_keeps_it_inactive(tmp_path):
    patch_path = (
        tmp_path
        / "profile_learning"
        / "default"
        / "project-1"
        / "patches"
        / "profilepatch-reject.json"
    )
    patch_path.parent.mkdir(parents=True)
    patch_path.write_text(
        json.dumps(
            {
                "schema_version": "0.1.0",
                "patch_id": "profilepatch-reject",
                "status": "candidate",
                "tenant_id": "default",
                "project_id": "project-1",
                "base_profile_name": "test_base",
                "changes": [],
                "evidence": {},
            }
        ),
        encoding="utf-8",
    )

    rejected = reject_profile_patch(
        tmp_path,
        "profilepatch-reject",
    )

    assert rejected["status"] == "rejected"
    assert list_profile_patches(tmp_path, status="rejected")[0][
        "patch_id"
    ] == "profilepatch-reject"


def test_profile_override_can_rollback_to_previous_version(tmp_path):
    profiles_root = tmp_path / "profiles"
    profiles_root.mkdir()
    base = json.loads(BASE_PROFILE.read_text(encoding="utf-8"))
    base["name"] = "rollback_base"
    (profiles_root / "rollback_base.json").write_text(
        json.dumps(base, ensure_ascii=False),
        encoding="utf-8",
    )
    patch_root = (
        tmp_path
        / "profile_learning"
        / "default"
        / "project-rollback"
        / "patches"
    )
    patch_root.mkdir(parents=True)
    for patch_id, threshold in (
        ("profilepatch-v1", 0.1),
        ("profilepatch-v2", 0.2),
    ):
        (patch_root / f"{patch_id}.json").write_text(
            json.dumps(
                {
                    "schema_version": "0.1.0",
                    "patch_id": patch_id,
                    "status": "candidate",
                    "tenant_id": "default",
                    "project_id": "project-rollback",
                    "base_profile_name": "rollback_base",
                    "changes": [
                        {
                            "target": "small_area_threshold",
                            "before": 0,
                            "after": threshold,
                            "evidence_count": 6,
                        }
                    ],
                    "evidence": {
                        "job_id": "job-rollback",
                        "drawing_digest": __import__("hashlib").sha256(
                            FIXTURE.read_bytes()
                        ).hexdigest(),
                        "target_counts": {
                            "panels": 6,
                            "holes": 4,
                            "numbers": 6,
                        },
                    },
                    "validation": None,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        accept_profile_patch(
            tmp_path,
            patch_id=patch_id,
            source_path=FIXTURE,
            profiles_root=profiles_root,
        )

    override_root = (
        tmp_path
        / "tenants"
        / "default"
        / "project-rollback"
        / "profile_overrides"
    )
    active = matching_profile_override(FIXTURE, override_root)
    assert active is not None
    assert active[1]["patch_id"] == "profilepatch-v2"

    rolled_back = rollback_profile_override(
        tmp_path,
        tenant_id="default",
        project_id="project-rollback",
        base_profile_name="rollback_base",
    )
    assert rolled_back["active_patch_id"] == "profilepatch-v1"
    previous = matching_profile_override(FIXTURE, override_root)
    assert previous is not None
    assert previous[0]["small_area_threshold"] == 0.1
