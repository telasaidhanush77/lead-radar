import pytest

from bench.pricing import PRICES, cost_per_1k
from bench.run import micro_f1, pairwise_prf


def test_pairwise_prf_perfect():
    assert pairwise_prf([5, 5, 9, 9, 7], [1, 1, 2, 2, 3]) == (1.0, 1.0, 1.0)


def test_pairwise_prf_all_singletons():
    # nothing merged: no false merges (precision 1 by convention), every duplicate missed
    assert pairwise_prf([None, None, None], [1, 1, 2]) == (1.0, 0.0, 0.0)


def test_pairwise_prf_known_case():
    # true pairs: (0,1),(2,3) = 2. predicted pairs: (0,1),(0,2),(1,2) = 3. correct: (0,1) = 1
    p, r, f1 = pairwise_prf([7, 7, 7, 8], [1, 1, 2, 2])
    assert (round(p, 4), r, round(f1, 4)) == (0.3333, 0.5, 0.4)


def test_pairwise_prf_none_is_never_a_match():
    assert pairwise_prf([None, None], [1, 1]) == (1.0, 0.0, 0.0)


def test_pairwise_prf_length_mismatch():
    with pytest.raises(ValueError):
        pairwise_prf([1], [1, 2])


def test_micro_f1():
    truth = [{"a", "b"}, set(), {"c"}]
    pred = [{"a"}, {"x"}, {"c"}]       # tp a,c = 2; fp x = 1; fn b = 1
    assert micro_f1(pred, truth) == pytest.approx(2 * 2 / (2 * 2 + 1 + 1))


def test_cost_per_1k():
    # 400 in + 50 out tokens per lead at $0.15 / $0.60 per 1M -> per 1k leads:
    # (400*0.15 + 50*0.60) / 1e6 * 1000 = 0.09
    assert cost_per_1k(0.15, 0.60, 400, 50) == pytest.approx(0.09)


def test_prices_have_sources():
    for provider in ("gemini", "groq"):
        entry = PRICES[provider]
        assert entry["source"].startswith("https://") and entry["checked"] == "2026-11-17"
        assert entry["in_per_m"] > 0 and entry["out_per_m"] > 0


def test_write_readme_replaces_only_the_results_block(tmp_path):
    from bench.run import write_readme

    readme = tmp_path / "README.md"
    readme.write_text("# T\n<!-- results:start -->\nold\n<!-- results:end -->\nkeep\n",
                      encoding="utf-8")
    write_readme("| new |", readme)
    assert readme.read_text(encoding="utf-8") == (
        "# T\n<!-- results:start -->\n| new |\n<!-- results:end -->\nkeep\n")


def test_markdown_reports_leakage_check():
    from bench.run import markdown

    d = {"leads": 10, "companies_true": 5, "companies_found": 5, "precision": 1, "recall": 1,
         "f1": 1, "seconds": 1, "dead_jobs": 0}
    results = {"date": "x", "commit": "y", "threshold": 92, "dedupe": d,
               "idempotency": {"alerts": 0, "crm": 0, "companies": 0}, "classifiers": {},
               "rules_generated_accuracy": 0.71}
    assert "71%" in markdown(results)
