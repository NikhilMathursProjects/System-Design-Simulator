import pytest
from sysdesign_engine.derivation.goal_dag import (
    Goal,
    GoalDAG,
    GoalKind,
    derive_api,
    derive_challenge,
)
from sysdesign_engine.library.component_loader import load_library
from sysdesign_engine.schemas.challenge_schema import APIContract, load_challenge

from test_challenge_schema import FIXTURE as SHORTENER_CHALLENGE


def contract(**kw) -> APIContract:
    return APIContract.model_validate(kw)


# ---------------------------------------------------------------------------
# simple contracts -> one goal; compute_units is cost-only
# ---------------------------------------------------------------------------


def test_simple_store_one_goal():
    dag = derive_api("setShort", contract(
        effect="store", entity="mapping", compute_units=2.0, write_units=1.5))
    assert dag.goals == (Goal(kind=GoalKind.STORE, entity="mapping", units=1.5),)
    assert dag.deps == (frozenset(),)
    assert dag.compute_units_cost == 2.0        # annotation, NOT a goal


def test_simple_retrieve_one_goal():
    dag = derive_api("getShort", contract(
        effect="retrieve", entity="mapping", compute_units=0.5, read_units=2.0))
    assert dag.goals == (Goal(kind=GoalKind.RETRIEVE, entity="mapping", units=2.0),)
    assert dag.compute_units_cost == 0.5


def test_simple_mutate_carries_op_and_condition():
    dag = derive_api("reserveItem", contract(
        effect="mutate", entity="inventory", op="decrement", condition="stock > 0"))
    (goal,) = dag.goals
    assert goal.kind is GoalKind.MUTATE
    assert goal.entity == "inventory"
    assert goal.op == "decrement"
    assert goal.condition == "stock > 0"


def test_simple_deliver_carries_recipient():
    dag = derive_api("sendMessage", contract(
        effect="deliver", entity="message", to="recipient"))
    (goal,) = dag.goals
    assert goal.kind is GoalKind.DELIVER
    assert goal.to == "recipient"


# ---------------------------------------------------------------------------
# composite contracts -> read -> compute -> write dependency chains
# ---------------------------------------------------------------------------


def test_enrich_record_three_node_chain():
    dag = derive_api("enrichRecord", contract(
        reads=[{"entity": "profile"}],
        compute_units=3.0,
        writes=[{"entity": "profile_enriched"}]))
    assert [g.kind for g in dag.goals] == [
        GoalKind.RETRIEVE, GoalKind.COMPUTE, GoalKind.STORE]
    assert dag.deps == (frozenset(), frozenset({0}), frozenset({1}))
    assert dag.goals[1].units == 3.0
    assert dag.compute_units_cost == 0.0        # compute is a real goal here


def test_multi_read_fan_in_and_multi_write_fan_out():
    dag = derive_api("mergeProfiles", contract(
        reads=[{"entity": "a"}, {"entity": "b", "read_units": 2.0}],
        compute_units=1.0,
        writes=[{"entity": "c"}, {"entity": "d"}]))
    kinds = [g.kind for g in dag.goals]
    assert kinds == [GoalKind.RETRIEVE, GoalKind.RETRIEVE,
                     GoalKind.COMPUTE, GoalKind.STORE, GoalKind.STORE]
    assert dag.deps[0] == frozenset() and dag.deps[1] == frozenset()  # parallel reads
    assert dag.deps[2] == frozenset({0, 1})                            # fan-in
    assert dag.deps[3] == dag.deps[4] == frozenset({2})                # fan-out
    assert dag.goals[1].units == 2.0


def test_reads_only_composite():
    dag = derive_api("aggregate", contract(
        reads=[{"entity": "events"}], compute_units=5.0))
    assert [g.kind for g in dag.goals] == [GoalKind.RETRIEVE, GoalKind.COMPUTE]
    assert dag.deps == (frozenset(), frozenset({0}))


def test_writes_only_composite():
    dag = derive_api("generate", contract(
        compute_units=2.0, writes=[{"entity": "report"}]))
    assert [g.kind for g in dag.goals] == [GoalKind.COMPUTE, GoalKind.STORE]
    assert dag.deps == (frozenset(), frozenset({0}))


# ---------------------------------------------------------------------------
# state helpers: eligibility, advancing, completion
# ---------------------------------------------------------------------------


def test_state_walk_through_chain():
    dag = derive_api("enrichRecord", contract(
        reads=[{"entity": "profile"}],
        compute_units=3.0,
        writes=[{"entity": "profile_enriched"}]))
    s = dag.initial_state()
    assert dag.eligible(s) == (0,)               # only the read
    s = dag.advance(s, 0)
    assert dag.eligible(s) == (1,)               # now the compute
    s = dag.advance(s, 1)
    assert dag.eligible(s) == (2,)               # now the store
    assert not dag.all_satisfied(s)
    s = dag.advance(s, 2)
    assert dag.all_satisfied(s)
    assert dag.eligible(s) == ()


def test_parallel_reads_both_eligible():
    dag = derive_api("merge", contract(
        reads=[{"entity": "a"}, {"entity": "b"}], compute_units=1.0))
    s = dag.initial_state()
    assert dag.eligible(s) == (0, 1)
    s = dag.advance(s, 1)                        # order is the player's business
    assert dag.eligible(s) == (0,)


def test_advance_rejects_ineligible_and_repeat():
    dag = derive_api("enrich", contract(
        reads=[{"entity": "p"}], compute_units=1.0, writes=[{"entity": "q"}]))
    s = dag.initial_state()
    with pytest.raises(ValueError):
        dag.advance(s, 1)                        # compute before its read
    s = dag.advance(s, 0)
    with pytest.raises(ValueError):
        dag.advance(s, 0)                        # already satisfied


def test_states_are_hashable_memo_keys():
    dag = derive_api("g", contract(effect="retrieve", entity="e"))
    s = dag.initial_state()
    memo = {("node-1", s): True}                 # what T-021 will do
    assert ("node-1", frozenset()) in memo
    assert hash(dag.goals[0]) == hash(Goal(kind=GoalKind.RETRIEVE, entity="e"))


def test_dag_rejects_malformed_deps():
    with pytest.raises(ValueError):
        GoalDAG(api="bad", goals=(Goal(kind=GoalKind.COMPUTE),),
                deps=(frozenset({0}),))          # self-dependency
    with pytest.raises(ValueError):
        GoalDAG(api="bad", goals=(Goal(kind=GoalKind.COMPUTE),),
                deps=(frozenset({5}),))          # out of range


# ---------------------------------------------------------------------------
# whole challenge
# ---------------------------------------------------------------------------


def test_derive_url_shortener_challenge():
    challenge = load_challenge(SHORTENER_CHALLENGE, load_library())
    dags = derive_challenge(challenge)
    assert set(dags) == {"setShort", "getShort"}
    assert dags["setShort"].goals == (
        Goal(kind=GoalKind.STORE, entity="mapping", units=1.0),)
    assert dags["setShort"].compute_units_cost == 2.0
    assert dags["getShort"].goals == (
        Goal(kind=GoalKind.RETRIEVE, entity="mapping", units=1.0),)
