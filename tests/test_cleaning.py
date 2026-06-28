from beamng_mcp.analysis import cleaning
from beamng_mcp.analysis.model import sample_from_row


def _clean_run(n=20, gy=1.0):
    return [sample_from_row({"t": i * 0.1, "gx": 0.1, "gy": gy}) for i in range(n)]


def test_clean_cornering_has_no_impacts():
    c = cleaning.detect_impacts(_clean_run(gy=1.2))  # 1.2 g is hard but real grip
    assert c.n_impacts == 0


def test_big_g_spike_is_flagged():
    # The live lap-5 case: a 16.9 g "corner" is a wall hit, not grip.
    samples = _clean_run()
    samples.insert(10, sample_from_row({"t": 1.05, "gy": 16.9}))
    c = cleaning.detect_impacts(samples)
    assert 10 in c.impacts
    assert c.n_impacts >= 1


def test_jerk_spike_is_flagged():
    # A violent single-sample g jump (impact transient) even if below the abs cap.
    samples = [sample_from_row({"gy": 0.5}), sample_from_row({"gy": 0.5}),
               sample_from_row({"gy": 4.0}), sample_from_row({"gy": 0.5})]
    c = cleaning.detect_impacts(samples, impact_g=10.0)  # disable abs cap to isolate jerk
    assert 2 in c.impacts


def test_clean_samples_removes_impacts():
    samples = _clean_run(n=10)
    samples.insert(5, sample_from_row({"gy": 16.0}))
    c = cleaning.detect_impacts(samples)
    cleaned = cleaning.clean_samples(samples, c)
    assert len(cleaned) == len(samples) - c.n_impacts
    assert all(s.combined_g < 3.5 for s in cleaned)
