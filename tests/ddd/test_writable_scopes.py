"""``loop.writable_scopes``: a [DATA]/[REGISTRY]/[SEED] fix stays IN when the loop can write it.

Why: canopy treats registry / data / seed fixes as another team's code by
default. On ACE (Connect Labs) the semantic registry, pipelines and synthetic
data are live data the loop edits through MCP, so parking those findings left
the loop "blocked" on things it could fix in seconds.
"""
from scripts.ddd import floor, loop_config

REG = {"detail": "Coverage tile shows 0%", "fix_recommendation": "[REGISTRY] fix the denominator", "route": "PRODUCT"}


def test_default_keeps_registry_out():
    assert floor.scope_detail(REG)["edit_scope"] == floor.OUT


def test_writable_registry_tag_stays_in():
    assert floor.scope_detail(REG, writable_scopes=("registry",))["edit_scope"] == floor.IN


def test_writable_fix_scope_stays_in():
    f = {"detail": "x", "fix_recommendation": "regen visits", "fix_scope": "data"}
    assert floor.scope_detail(f)["edit_scope"] == floor.OUT
    assert floor.scope_detail(f, writable_scopes=["DATA"])["edit_scope"] == floor.IN


def test_any_unwritable_tag_still_out():
    f = {**REG, "fix_recommendation": "[REGISTRY] fix it; [EXTERNAL] and the HQ export"}
    d = floor.scope_detail(f, writable_scopes=("registry",))
    assert d["edit_scope"] == floor.OUT and "EXTERNAL" in d["reason"]


def test_third_party_maps_to_external():
    f = {"detail": "x", "fix_recommendation": "[THIRD-PARTY] vendor map tiles"}
    assert floor.scope_detail(f, writable_scopes=("external",))["edit_scope"] == floor.IN


def test_classify_floor_and_edit_scope_pass_it_through():
    assert floor.edit_scope(REG, writable_scopes=("registry",))[0] == floor.IN


def test_config_parses_list_and_string():
    c = loop_config.parse({"loop": {"writable_scopes": ["Data", "registry", "data"]}})
    assert c.loop.writable_scopes == ("data", "registry")
    assert loop_config.parse({"loop": {"writable_scopes": "seed, registry"}}).loop.writable_scopes == ("seed", "registry")
    assert loop_config.parse({}).loop.writable_scopes == ()
