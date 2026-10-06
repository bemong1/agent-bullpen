"""Scenario generator: axes -> synthetic HOME -> truth (oracle) -> what the board shows (observe) -> pass / miss / wrong (run).

    python3 -m tools.scenarios.run            # pass/miss/wrong table on stdout
    python3 -m tools.scenarios.run --md       # also print the table of red cells (--md FILE writes it to a file)

Everything here is synthetic: no real record, path, id or time is read or copied. oracle.py, contract.py, plan.py and axes.py never import board/ (observe.py, the adapter that reads what the board shows, does; `OracleIsIndependent` checks the four; contract.py is the
truth of the 0.3.0 contract, plan.py says what the records of a scene hold, in the shape contract.truth reads).
"""
