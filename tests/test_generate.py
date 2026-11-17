import json
from collections import Counter
from pathlib import Path

from app.classify import INDUSTRIES, SIGNALS
from bench.generate import generate

LABELLED = Path(__file__).resolve().parent.parent / "bench" / "labelled.jsonl"


def test_generate_is_deterministic():
    assert generate(1, 50) == generate(1, 50)
    assert generate(1, 50) != generate(2, 50)


def test_entities_have_duplicates():
    per_entity = Counter(lead["_entity"] for lead in generate(1, 300))
    assert len(per_entity) == 300
    assert sum(n >= 2 for n in per_entity.values()) / 300 >= 0.30


def test_keys_unique_and_seed_scoped():
    keys = [lead["_key"] for lead in generate(1, 300)]
    assert len(keys) == len(set(keys)) and all(k.startswith("s1-") for k in keys)


def test_messy_variants_present():
    leads = generate(1, 300)
    assert any(lead.get("email", "").endswith("@gmail.com") for lead in leads)
    assert any("website" not in lead for lead in leads)
    assert any("country" not in lead for lead in leads)
    assert any(lead.get("company", "").isupper() for lead in leads)


def test_ground_truth_labels_valid():
    for lead in generate(1, 300):
        assert lead["_labels"]["industry"] in INDUSTRIES
        assert set(lead["_labels"]["signals"]) <= set(SIGNALS)


def test_many_templates_per_industry():
    descriptions = {}
    for lead in generate(1, 300) + generate(2, 300):
        if "description" in lead:
            first = lead["description"].split(". ")[0]
            descriptions.setdefault(lead["_labels"]["industry"], set()).add(first)
    assert all(len(v) >= 8 for v in descriptions.values()), {k: len(v) for k, v in
                                                            descriptions.items()}


def test_labelled_set_valid():
    rows = [json.loads(line) for line in LABELLED.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    assert len(rows) == 60
    assert all(r["industry"] in INDUSTRIES and set(r["signals"]) <= set(SIGNALS) for r in rows)
    assert len({r["text"] for r in rows}) == 60
    generated = {lead.get("description") for seed in (1, 2) for lead in generate(seed, 300)}
    assert not generated & {r["text"] for r in rows}
