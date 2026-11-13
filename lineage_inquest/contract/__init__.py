"""Declarative lineage contracts: loading, comparison, classification."""

from .classify import classify
from .compare import compare_contracts
from .loader import load_contracts

__all__ = ["classify", "compare_contracts", "load_contracts"]
