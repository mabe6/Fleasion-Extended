"""Regression test for the exact user-supplied CurveAnimation fixture.

This test intentionally preserves the animation's unusual encoded curve value;
it must not normalize or discard it merely because it is large.
"""
from pathlib import Path
import core
import animation_data
import preview
from fleasion_port.cache.rbxm_parser import parse_rbxm

ASSET = Path(__file__).parent / "tests" / "assets" / "ImportedAnimationClip3_73885081417917.rbxm"
EXPECTED_EXTREME_Z = -1.0000000331813535e32


def main():
    payload = ASSET.read_bytes()
    assert core.kind_of(payload) == "curve-anim"
    preview_info = preview.parse_rbxm_animation(payload)
    assert preview_info.kind == "CurveAnimation"
    parsed_instances = parse_rbxm(payload)
    assert any(instance.class_name == "CurveAnimation" for instance in parsed_instances.values())
    anim = animation_data.parse_curve_animation(payload)
    assert anim.loop is True
    assert anim.priority == "Action4"
    assert abs(anim.duration - (1 / 600)) < 1e-12
    assert "HumanoidRootPart" in anim.joint_paths
    z_curve = anim.positions["LowerTorso"]["Z"]
    assert len(z_curve.keys) == 2
    assert z_curve.keys[-1].value == EXPECTED_EXTREME_Z, z_curve.keys[-1].value
    assert z_curve.keys[-1].time == anim.duration
    print("PASS supplied animation identity and metadata")
    print("PASS Fleasion binary parser identifies the CurveAnimation root")
    print("PASS supplied animation short duration preserved")
    print("PASS LowerTorso.Z extreme value preserved without normalization")
    print("ALL SUPPLIED ANIMATION REGRESSION TESTS PASSED")


if __name__ == "__main__":
    main()
