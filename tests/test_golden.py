"""Schema checks run on made-up data. Real labels live in data/golden/ and are hand-written."""

import copy

import pytest
import yaml
from pydantic import ValidationError

from covenant_monitor.golden import GOLDEN_DIR, GoldenCompany, label_files, load_all, load_file

FAKE = {
    "company": {"ticker": "FAKE", "cik": 1, "name": "Example Corp"},
    "agreement": {
        "accession": "0000000001-20-000001",
        "exhibit": "EX-10.1",
        "url": "https://www.sec.gov/Archives/edgar/data/1/000000000120000001/ex101.htm",
        "agreement_date": "2020-01-01",
    },
    "covenants": [
        {
            "type": "max_leverage",
            "description": "Leverage Ratio",
            "thresholds": [
                {"ratio": 4.0, "effective_from": None},
                {"ratio": 3.5, "effective_from": "2021-01-01"},
            ],
            "test_frequency": "quarterly",
            "section": "Section 1.1",
            "quote": "made-up quote",
        }
    ],
    "ebitda_addbacks": [{"name": "made-up add-back", "section": "Section 1.2", "cap": None}],
    "latest_fiscal_year": {
        "period_end": "2021-12-31",
        "inputs": {"ebitda": {"value": 1.0, "source": "made up"}},
        "expected": [{"covenant": "max_leverage", "ratio": 1.0, "threshold": 2.0, "headroom": 1.0}],
    },
    "questions": [
        {"question": "q1", "answer": "a1", "section": "Section 1.1"},
        {"question": "q2", "answer": "a2"},
        {"question": "q3", "answer": "a3"},
        {"question": "q4", "trap": True},
        {"question": "q5", "trap": True},
    ],
}


def fake(**changes):
    data = copy.deepcopy(FAKE)
    for path, value in changes.items():
        *parents, key = path.split("__")
        target = data
        for p in parents:
            target = target[int(p)] if p.isdigit() else target[p]
        if key.isdigit():
            target[int(key)] = value
        else:
            target[key] = value
    return data


def test_fake_company_is_valid():
    GoldenCompany.model_validate(FAKE)


@pytest.mark.parametrize(
    "changes",
    [
        {"questions": FAKE["questions"][:4]},
        {"questions__2": {"question": "q3", "trap": True}},
        {"questions__3": {"question": "q4", "trap": True, "answer": "should be empty"}},
        {"questions__0": {"question": "q1", "answer": None}},
        {"agreement__url": "https://example.com/Archives/edgar/data/1/x.htm"},
        {"agreement__accession": "1-20-1"},
        {"agreement__exhibit": "Exhibit 10.1"},
        {"company__cik": "abc"},
        {"covenants__0__type": "max_vibes"},
        {"covenants__0__test_frequency": "whenever"},
        {"covenants__0__thresholds": []},
        {
            "covenants__0__thresholds": [
                {"ratio": 3.5, "effective_from": "2022-01-01"},
                {"ratio": 4.0, "effective_from": "2021-01-01"},
            ]
        },
        {"covenants__0__thresholds": [{"ratio": 4.0}, {"ratio": 3.5}]},
        {"covenants__0__thresholds": [{"ratio": -1.0}]},
        {"latest_fiscal_year__expected__0__covenant": "min_interest_coverage"},
        {"unexpected_field": 1},
    ],
)
def test_rejects_malformed_labels(changes):
    with pytest.raises(ValidationError):
        GoldenCompany.model_validate(fake(**changes))


def test_file_name_must_match_ticker(tmp_path):
    (tmp_path / "OTHER.yaml").write_text(yaml.safe_dump(FAKE))
    with pytest.raises(ValueError, match="FAKE.yaml"):
        load_file(tmp_path / "OTHER.yaml")


def test_loader_skips_template_and_keys_by_ticker(tmp_path):
    (tmp_path / "FAKE.yaml").write_text(yaml.safe_dump(FAKE))
    (tmp_path / "_template.yaml").write_text("company:\n  ticker:\n")
    assert list(load_all(tmp_path)) == ["FAKE"]


def test_template_is_not_a_valid_label():
    """The template has blanks, so a copy that wasn't filled in fails validation."""
    data = yaml.safe_load((GOLDEN_DIR / "_template.yaml").read_text())
    with pytest.raises(ValidationError):
        GoldenCompany.model_validate(data)


@pytest.mark.parametrize("path", label_files(), ids=lambda p: p.name)
def test_every_hand_written_label_file_is_valid(path):
    load_file(path)
