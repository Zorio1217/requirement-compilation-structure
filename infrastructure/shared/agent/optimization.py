"""Opt-in execution policy scoped to one compiler run, never to provider settings."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

@dataclass(frozen=True)
class Policy:
    enabled: bool = False
    max_calls: int = 24
    max_reads: int = 6
    group_size: int = 3

_policy = ContextVar('compiler_policy', default=Policy())

def policy():
    return _policy.get()

@contextmanager
def optimized_run(enabled=False, *, max_calls=24, max_reads=6, group_size=3):
    if min(max_calls, max_reads, group_size) < 1:
        raise ValueError('Optimization budgets must be positive')
    token = _policy.set(Policy(enabled, max_calls, max_reads, group_size))
    try:
        yield
    finally:
        _policy.reset(token)
