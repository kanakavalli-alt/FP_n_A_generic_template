from fpa_agent.agents import prompts
from fpa_agent.semantic.repository import YamlRepository, _build_catalog
from fpa_agent.semantic.search import search
from fpa_agent.semantic.validator import validate_against_sources, validate_catalog
from tests.conftest import AS_OF, DEMO_DIR


def test_demo_catalog_has_no_errors(catalog):
    assert [str(i) for i in validate_catalog(catalog) if i.level == "error"] == []


def test_live_schema_check_passes(catalog, run):
    assert [str(i) for i in run(validate_against_sources(catalog))] == []


def _broken():
    raw = YamlRepository(DEMO_DIR).load_raw()
    m = next(r for r in raw["metrics"] if r["metric_id"] == "leads")
    m["measure"] = "SUM(leads); DROP TABLE x"
    m["dimensions"] = {"planet": "planet"}
    a = next(r for r in raw["analyses"] if r["analysis_id"] == "census_bridge")
    a["inputs"] = {"level": "active_students"}                      # flows missing
    b = next(r for r in raw["analyses"] if r["analysis_id"] == "new_starts_drivers")
    b["dimension"] = "modality"                                     # metrics don't have it
    raw["metrics"].append({"metric_id": "loop_a", "term": "A", "metric_type": "derived", "formula": "{loop_b}"})
    raw["metrics"].append({"metric_id": "loop_b", "term": "B", "metric_type": "derived", "formula": "{loop_a}"})
    raw["sources"][0]["connection"] = {"mode": "local", "password": "hunter2"}
    return _build_catalog(raw)


def test_validator_catches_real_mistakes():
    msgs = "\n".join(str(i) for i in validate_catalog(_broken()) if i.level == "error")
    for expected in ["statement separator", "dimension 'planet'", "needs inputs.flows",
                     "has no dimension 'modality'", "circular", "looks like a secret"]:
        assert expected in msgs, expected


def test_live_schema_check_catches_typos(catalog, run):
    raw = YamlRepository(DEMO_DIR).load_raw()
    next(r for r in raw["metrics"] if r["metric_id"] == "leads")["measure"] = "SUM(lead_count)"
    issues = run(validate_against_sources(_build_catalog(raw)))
    assert any("lead_count" in str(i) for i in issues)


def test_search_resolves_aliases_and_phrases(catalog):
    assert search(catalog, "census")[0]["id"] == "active_students"
    assert search(catalog, "why are starts down vs last year")[0]["id"] == "new_starts"
    assert search(catalog, "net sales")[0]["id"] == "net_revenue"
    hit = next(h for h in search(catalog, "marketing spend") if h["type"] == "metric")
    assert hit["status"] == "blocked" and hit["queryable"] is False
    assert search(catalog, "xyzzy plugh") == []


def test_root_prompt_is_built_from_the_semantic_layer(catalog):
    text = prompts.root_instruction(catalog, AS_OF)
    for needle in ["Northwind Learning", "Northwind FP&A Analyst", "| new_starts | New starts |",
                   "census_bridge", "Re-entry", "Last CLOSED month: Sep 2026", "Grounding rules",
                   "Copy `drivers_table` exactly"]:
        assert needle in text, needle


def test_explorer_prompt_lists_allowed_tables(catalog):
    text = prompts.explorer_instruction(catalog, AS_OF)
    assert "enrollment_funnel" in text and "exploratory" in text
