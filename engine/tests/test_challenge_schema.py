import json
from pathlib import Path

import pytest
from sysdesign_engine.library.component_loader import load_library
from sysdesign_engine.schemas.challenge_schema import (
    APIContract,
    ChallengeError,
    Traffic,
    load_challenge,
    load_challenge_dict,
)

CHALLENGES_DIR = (Path(__file__).parents[1] / "src" / "sysdesign_engine"/ "challenges")
FIXTURE = CHALLENGES_DIR / "url_shortener.json"
ALL_CHALLENGE_FILES = sorted(CHALLENGES_DIR.glob("*.json"))


@pytest.fixture(scope="module")
def library():
    return load_library()


@pytest.fixture()
def shortener() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))




@pytest.mark.parametrize("path", ALL_CHALLENGE_FILES, ids=lambda p: p.stem)
def test_every_challenge_pack_file_parses(path, library):
    c = load_challenge(path, library)
    assert c.apis, f"{path.name} declares no APIs"
    assert c.entities, f"{path.name} declares no entities"
    if c.traffic.mix is not None:
        assert set(c.traffic.mix) == set(c.apis), (
            f"{path.name}: traffic.mix should cover every API")


def test_url_shortener_fixture_parses(library):
    c = load_challenge(FIXTURE, library)
    assert set(c.apis) == {"setShort", "getShort"}
    assert c.entities["mapping"].record_kb == 0.3
    assert c.traffic.rps == 500
    assert c.traffic.key_dist_alpha == 1.1
    assert c.slos.per_api["getShort"].p99_ms == 100
    assert c.slos.per_api["setShort"].p99_ms == 300
    assert c.slos.availability == 0.999
    assert c.slos.durability == 0.99999
    assert c.slos.monthly_budget_usd == 800
    assert c.slos.completion_threshold == 0.999  # Â§17.6 default


def test_completion_threshold_overridable(shortener, library):
    shortener["slos"]["completion_threshold"] = 0.95
    c = load_challenge_dict(shortener, library)
    assert c.slos.completion_threshold == 0.95


#forbidden-field validator
@pytest.mark.parametrize("mutate", [
    lambda raw: raw["entities"].update({"postgres": {"record_kb": 1.0, "keyspace": 10}}),
    lambda raw: raw["apis"].update({"redis": {"effect": "retrieve", "entity": "mapping"}}),
    lambda raw: raw["apis"]["setShort"].update({"entity": "s3"}) or
        raw["entities"].update({"s3": {"record_kb": 1.0, "keyspace": 10}}),
])
def test_component_name_anywhere_is_rejected(shortener, library, mutate):
    mutate(shortener)
    with pytest.raises(ChallengeError) as exc_info:
        load_challenge_dict(shortener, library)
    assert "invariant 2" in str(exc_info.value)


def test_leakage_check_skipped_without_library(shortener):
    shortener["entities"]["postgres"] = {"record_kb": 1.0, "keyspace": 10}
    load_challenge_dict(shortener)  # no library -> no leakage check, still parses


def test_innocent_names_pass(shortener, library):
    shortener["entities"]["profile"] = {"record_kb": 2.0, "keyspace": 1000}
    shortener["apis"]["getProfile"] = {"effect": "retrieve", "entity": "profile"}
    load_challenge_dict(shortener, library)



#----------------------------------------------API contract shapes----------------------------------------

def test_simple_contract_requires_entity():
    with pytest.raises(ValueError):
        APIContract.model_validate({"effect": "store"})


def test_unknown_effect_rejected():
    with pytest.raises(ValueError):
        APIContract.model_validate({"effect": "teleport", "entity": "mapping"})


def test_deliver_requires_to():
    with pytest.raises(ValueError):
        APIContract.model_validate({"effect": "deliver", "entity": "message"})
    APIContract.model_validate(
        {"effect": "deliver", "entity": "message", "to": "recipient"})


def test_to_only_on_deliver():
    with pytest.raises(ValueError):
        APIContract.model_validate(
            {"effect": "store", "entity": "mapping", "to": "recipient"})


def test_mutate_requires_op():
    with pytest.raises(ValueError):
        APIContract.model_validate({"effect": "mutate", "entity": "inventory"})
    APIContract.model_validate(
        {"effect": "mutate", "entity": "inventory","op": "decrement", "condition": "counter > 0"})


def test_op_condition_only_on_mutate():
    with pytest.raises(ValueError):
        APIContract.model_validate(
            {"effect": "retrieve", "entity": "mapping", "op": "decrement"})


def test_composite_contract_parses():
    c = APIContract.model_validate({
        "reads": [{"entity": "profile"}],
        "compute_units": 3.0,
        "writes": [{"entity": "profile_enriched"}],
    })
    assert c.referenced_entities() == {"profile", "profile_enriched"}


def test_composite_rejects_effect_and_entity():
    with pytest.raises(ValueError):
        APIContract.model_validate(
            {"effect": "store", "reads": [{"entity": "profile"}]})
    with pytest.raises(ValueError):
        APIContract.model_validate(
            {"entity": "profile", "reads": [{"entity": "profile"}]})


def test_composite_needs_reads_or_writes():
    with pytest.raises(ValueError):
        APIContract.model_validate({"reads": [], "writes": []})


def test_extra_field_rejected():
    with pytest.raises(ValueError):
        APIContract.model_validate(
            {"effect": "store", "entity": "mapping", "topology_hint": "use a cache"})



#--------------------------------------------traffic shapes--------------------------------------------------
def test_request_response_requires_rps():
    with pytest.raises(ValueError):
        Traffic.model_validate({"shape": "request_response"})


def test_burst_parses_and_validates_profile():
    t = Traffic.model_validate(
        {"shape": "burst", "rps_profile": [[0, 100], [60, 30000], [120, 100]]})
    assert t.rps_profile[1] == [60, 30000]
    with pytest.raises(ValueError):
        Traffic.model_validate({"shape": "burst", "rps_profile": [[0, 100, 5]]})
    with pytest.raises(ValueError):
        Traffic.model_validate({"shape": "burst", "rps_profile": [[0, -5]]})


def test_session_requires_all_session_fields():
    with pytest.raises(ValueError):
        Traffic.model_validate({"shape": "session", "connect_rps": 100})
    Traffic.model_validate(
        {"shape": "session", "connect_rps": 100,
         "session_duration_s": 300, "messages_per_sec": 2})


def test_cross_shape_fields_rejected():
    with pytest.raises(ValueError):
        Traffic.model_validate(
            {"shape": "request_response", "rps": 100, "rps_profile": [[0, 5]]})


def test_unknown_shape_rejected():
    with pytest.raises(ValueError):
        Traffic.model_validate({"shape": "steady", "rps": 100})


def test_bad_key_dist_rejected():
    with pytest.raises(ValueError):
        Traffic.model_validate(
            {"shape": "request_response", "rps": 100, "key_dist": "uniform"})


def test_zipf_alpha_parsed():
    t = Traffic.model_validate(
        {"shape": "request_response", "rps": 100, "key_dist": "zipf(0.8)"})
    assert t.key_dist_alpha == 0.8


def test_mix_must_sum_to_one():
    with pytest.raises(ValueError):
        Traffic.model_validate(
            {"shape": "request_response", "rps": 100,
             "mix": {"a": 0.5, "b": 0.3}})



#----------------------------------------------cross-references---------------------------------------------
def test_api_referencing_unknown_entity_rejected(shortener, library):
    shortener["apis"]["getShort"]["entity"] = "nonexistent"
    with pytest.raises(ChallengeError) as exc_info:
        load_challenge_dict(shortener, library)
    assert "unknown entities" in str(exc_info.value)


def test_mix_referencing_unknown_api_rejected(shortener, library):
    shortener["traffic"]["mix"] = {"getShort": 0.5, "ghostApi": 0.5}
    with pytest.raises(ChallengeError) as exc_info:
        load_challenge_dict(shortener, library)
    assert "unknown apis" in str(exc_info.value)


def test_slo_referencing_unknown_api_rejected(shortener, library):
    shortener["slos"]["ghostApi"] = {"p99_ms": 50}
    with pytest.raises(ChallengeError) as exc_info:
        load_challenge_dict(shortener, library)
    assert "unknown apis" in str(exc_info.value)


def test_composite_challenge_end_to_end(library):
    raw = {
        "entities": {
            "profile": {"record_kb": 2.0, "keyspace": 100000},
            "profile_enriched": {"record_kb": 4.0, "keyspace": 100000},
        },
        "apis": {
            "enrichRecord": {
                "reads": [{"entity": "profile"}],
                "compute_units": 3.0,
                "writes": [{"entity": "profile_enriched"}],
            }
        },
        "traffic": {"shape": "request_response", "rps": 200,
                    "mix": {"enrichRecord": 1.0}},
        "slos": {"enrichRecord": {"p99_ms": 500}, "availability": 0.99},
    }
    c = load_challenge_dict(raw, library)
    assert c.apis["enrichRecord"].compute_units == 3.0
