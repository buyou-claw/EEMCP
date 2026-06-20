"""JS220 state machine.

The JS220 has a real multi-step lifecycle (connect → configure →
start streaming → collect stats → stop streaming) with rules about
which steps are legal from which state. The state machine makes
those rules explicit so the MCP tools can call illegal-transition
errors instead of silently producing garbage.

States:

    DISCONNECTED  -> no device, no scan cache
    IDLE          -> device open, sensor off
    STREAMING     -> sensor on, statistics_callbacks firing

Transitions:

    DISCONNECTED --connect()--------> IDLE
    IDLE         --start()---------> STREAMING
    STREAMING    --stop()----------> IDLE
    IDLE         --disconnect()----> DISCONNECTED
    STREAMING    --disconnect()----> DISCONNECTED   (force stop)
    any          --error-----------> ERROR
    ERROR        --reset()---------> DISCONNECTED
"""
from __future__ import annotations
from enum import Enum
from typing import Optional


class State(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    IDLE = "IDLE"
    STREAMING = "STREAMING"
    ERROR = "ERROR"


# Allowed transitions: from_state -> set of allowed to_states
_ALLOWED: dict[State, set[State]] = {
    State.DISCONNECTED: {State.IDLE, State.ERROR},
    State.IDLE: {State.STREAMING, State.DISCONNECTED, State.ERROR},
    State.STREAMING: {State.IDLE, State.DISCONNECTED, State.ERROR},
    State.ERROR: {State.DISCONNECTED},
}


class StateError(RuntimeError):
    """Raised on an illegal state transition."""


class StateMachine:
    """Tiny guard for the JS220's connect/start/stop lifecycle."""

    def __init__(self) -> None:
        self._state: State = State.DISCONNECTED
        self._last_error: Optional[str] = None

    @property
    def state(self) -> State:
        return self._state

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    def is_(self, *states: State) -> bool:
        return self._state in states

    def require(self, *states: State) -> None:
        if self._state not in states:
            raise StateError(
                f"operation requires state in {[s.value for s in states]}, "
                f"current state is {self._state.value}"
            )

    def transition(self, to: State) -> None:
        if to not in _ALLOWED.get(self._state, set()):
            raise StateError(
                f"illegal transition {self._state.value} -> {to.value}"
            )
        self._state = to
        if to != State.ERROR:
            self._last_error = None

    def mark_error(self, msg: str) -> None:
        self._state = State.ERROR
        self._last_error = msg
