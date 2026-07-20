"""T-013: contract -> goal DAG derivation (SPEC section 4).

Pure functions. Input: an APIContract (challenge language: EFFECTS).
Output: a GoalDAG (engine language: the per-request checklist).

Rules:
- simple store/retrieve/mutate/deliver -> ONE goal node carrying its params.
  compute_units on a simple contract is a COST-ONLY annotation (charged at
  the first compute-capable node traversed, or never) -- NOT a goal, so a
  client wired straight to a DB still completes.
- composite (reads/writes) -> parallel RETRIEVE goals (no edges between
  them), one COMPUTE goal depending on ALL reads, parallel STORE goals each
  depending on the compute. Data dependencies only -- never a component
  sequence.

Goal state is a frozenset of satisfied goal indices: hashable and tiny, because T-021 memoizes feasibility on (node, goal_state) keys.

Satisfaction constraints (WHERE a goal may be advanced) are tagged here via
GoalKind but enforced by feasibility/routing:
- RETRIEVE / STORE: stores (landing-set semantics live in T-020)
- COMPUTE: any compute-capable node
- MUTATE: only a store in the entity's landing set (per-key serialized)
- DELIVER: only at the recipient client's session
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, FrozenSet, Optional, Tuple

from sysdesign_engine.schemas.challenge_schema import APIContract, ChallengeRecord


class GoalKind(str, Enum):
    RETRIEVE = "retrieve"
    COMPUTE = "compute"
    STORE = "store"
    MUTATE = "mutate"
    DELIVER = "deliver"


@dataclass(frozen=True)
class Goal:
    """One node of the checklist. Immutable; equality/hash by value."""
    kind: GoalKind
    entity: Optional[str] = None        # None only for COMPUTE
    units: float = 1.0                  # workload multiplier charged where advanced
    to: Optional[str] = None            # DELIVER: recipient session role
    op: Optional[str] = None            # MUTATE: e.g. "decrement"
    condition: Optional[str] = None     # MUTATE: e.g. "stock > 0"


GoalState = FrozenSet[int]


@dataclass(frozen=True)
class GoalDAG:
    """The derived checklist for one API. goals[i] depends on deps[i]
    (indices that must be satisfied before i becomes eligible)."""
    api: str
    goals: Tuple[Goal, ...]
    deps: Tuple[FrozenSet[int], ...]
    # cost-only compute annotation for SIMPLE contracts (0.0 on composites,
    # where compute is a real goal instead)
    compute_units_cost: float = 0.0

    def __post_init__(self):
        if len(self.goals) != len(self.deps):
            raise ValueError(
                f"api '{self.api}': {len(self.goals)} goals but {len(self.deps)} dep sets")
        for i, dep in enumerate(self.deps):
            bad = [j for j in dep if not (0 <= j < len(self.goals)) or j == i]
            if bad:
                raise ValueError(
                    f"api '{self.api}': goal {i} has invalid deps {sorted(bad)}")

    # ---- state helpers (frozenset of satisfied indices) ----

    def initial_state(self) -> GoalState:
        return frozenset()

    def eligible(self, state: GoalState) -> Tuple[int, ...]:
        """Unsatisfied goals whose dependencies are all satisfied."""
        return tuple(
            i for i in range(len(self.goals))
            if i not in state and self.deps[i] <= state
        )

    def advance(self, state: GoalState, goal_index: int) -> GoalState:
        if goal_index in state:
            raise ValueError(f"api '{self.api}': goal {goal_index} already satisfied")
        if not self.deps[goal_index] <= state:
            raise ValueError(
                f"api '{self.api}': goal {goal_index} not eligible -- "
                f"unmet deps {sorted(self.deps[goal_index] - state)}")
        return state | {goal_index}

    def all_satisfied(self, state: GoalState) -> bool:
        return len(state) == len(self.goals)


def derive_api(api_name: str, contract: APIContract) -> GoalDAG:
    """One APIContract -> one GoalDAG. See module docstring for the rules."""
    is_composite = contract.reads is not None or contract.writes is not None

    if not is_composite:
        goal = _simple_goal(contract)
        return GoalDAG(
            api=api_name,
            goals=(goal,),
            deps=(frozenset(),),
            compute_units_cost=contract.compute_units,
        )

    goals = []
    deps = []

    read_indices = []
    for r in contract.reads or []:
        read_indices.append(len(goals))
        goals.append(Goal(kind=GoalKind.RETRIEVE, entity=r.entity, units=r.read_units))
        deps.append(frozenset())

    # composite compute is a real routing goal, gated on ALL reads
    compute_index = len(goals)
    goals.append(Goal(kind=GoalKind.COMPUTE, units=contract.compute_units))
    deps.append(frozenset(read_indices))

    for w in contract.writes or []:
        goals.append(Goal(kind=GoalKind.STORE, entity=w.entity, units=w.write_units))
        deps.append(frozenset({compute_index}))

    return GoalDAG(api=api_name, goals=tuple(goals), deps=tuple(deps))


def _simple_goal(contract: APIContract) -> Goal:
    if contract.effect == "store":
        return Goal(kind=GoalKind.STORE, entity=contract.entity,
                    units=contract.write_units)
    if contract.effect == "retrieve":
        return Goal(kind=GoalKind.RETRIEVE, entity=contract.entity,
                    units=contract.read_units)
    if contract.effect == "mutate":
        return Goal(kind=GoalKind.MUTATE, entity=contract.entity,
                    units=contract.write_units,
                    op=contract.op, condition=contract.condition)
    if contract.effect == "deliver":
        return Goal(kind=GoalKind.DELIVER, entity=contract.entity,
                    units=contract.write_units, to=contract.to)
    raise ValueError(f"unknown effect {contract.effect!r}")   # unreachable post-schema


def derive_challenge(challenge: ChallengeRecord) -> Dict[str, GoalDAG]:
    """Every API's DAG, keyed by API name. This dict is what feasibility
    (T-020/T-021), routing (T-030) and grading consume."""
    return {name: derive_api(name, contract)
            for name, contract in challenge.apis.items()}
