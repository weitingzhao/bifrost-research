"""Suggestion ledger — issue settleable suggestions, settle them (stage 2).

Design: ``DESIGN-suggestion-ledger-and-settlement-2026-10-05.md`` (Owner-approved
2026-10-05). A suggestion is a complete, settleable structure — legs to the
contract, management rules, expectation and a frozen snapshot of its inputs —
written once to ``research.suggestion`` and never changed. Settlement walks it
with the option simulator and appends ``research.suggestion_settlement`` rows.

- ``contract``: the suggestion record, its validation and its identity.
- ``config``: the frozen threshold set and the mechanical sources' rules.
- ``issue``: the mechanical sources (``baseline``, ``simulator``).
- ``settle``: the ``model`` / ``model_stress`` / ``baseline_paired`` settlement.
- ``store``: reads and appends.

D10 BLOCKED — a suggestion is a record; nothing here reaches an order.
"""
