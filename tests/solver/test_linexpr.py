from custom_components.zenmaster.solver.solver import LpSolver, LinExpr


def _lp():
    return LpSolver(track=lambda *a, **k: None, state=lambda *a, **k: None, horizon=96)


def test_linexpr_ops():
    e = 2.0 * (LinExpr({0: 1.0}) - LinExpr({1: 1.0})) + LinExpr({0: 0.5})
    assert e.terms == {0: 2.5, 1: -2.0}
    assert (-LinExpr({3: 4.0})).terms == {3: -4.0}


def test_column_getitem_is_linexpr():
    lp = _lp()
    a = lp.column("a")
    assert isinstance(a[5], LinExpr)
    assert a[5].terms == {a.index + 5: 1.0}


def test_rowdelta_algebra():
    lp = _lp()
    a = lp.column("a")
    b = lp.column("b")
    lp.rowDelta(lambda t1, t2: a[t1] - a[t2] - 2.0 * b[t2])
    row, lo, hi = lp._rows[0]  # couples t=1 and t=0
    assert row.terms == {a.index + 1: 1.0, a.index + 0: -1.0, b.index + 0: -2.0}


def test_rowquarter_algebra():
    lp = _lp()
    s = lp.column("s", tags=("supply",))
    d = lp.column("d", tags=("draw",))
    lp.rowQuarter({"supply": 1.0, "draw": -1.0}, lo=0.0, hi=0.0)
    row, lo, hi = lp._rows[0]  # t=0
    assert row.terms == {s.index: 1.0, d.index: -1.0}
