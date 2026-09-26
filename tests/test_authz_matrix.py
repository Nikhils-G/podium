"""Roles × sensitive endpoints. Every cell is a promise the backend keeps regardless of the UI."""

import pytest

from tests.conftest import SLUG

S = f"/api/v1/events/{SLUG}"

MATRIX = [
    # (method, path, role, expected)
    ("GET", f"{S}/judges/me/reviews", "judge_a", 200),
    ("GET", f"{S}/judges/me/reviews", "judge_b", 200),
    ("GET", f"{S}/judges/me/reviews", "participant", 403),
    ("GET", f"{S}/judges/me/reviews", None, 401),
    ("GET", f"{S}/judges/jdg_24/reviews", "judge_a", 200),  # jdg_24 is judge_a
    ("GET", f"{S}/judges/jdg_24/reviews", "judge_b", 403),
    ("GET", f"{S}/judges/jdg_24/reviews", "participant", 403),
    ("GET", f"{S}/judges/jdg_24/reviews", "organizer", 200),
    ("GET", f"{S}/judges/jdg_24/reviews", "admin", 200),
    ("GET", f"{S}/judges/jdg_24/reviews", None, 401),
    ("GET", f"{S}/judges", "judge_a", 403),
    ("GET", f"{S}/judges", "organizer", 200),
    ("GET", f"{S}/assignments", "judge_a", 403),
    ("GET", f"{S}/assignments", "organizer", 200),
    ("GET", f"{S}/exports/scores.csv", "participant", 403),
    ("GET", f"{S}/exports/scores.csv", "judge_a", 403),
    ("GET", f"{S}/exports/scores.csv", "organizer", 200),
    ("GET", f"{S}/exports/reviews.csv", "organizer", 200),
    ("GET", f"{S}/audit", "judge_a", 403),
    ("GET", f"{S}/audit", "organizer", 200),
    ("GET", f"{S}/results", None, 404),  # hidden until published
    ("GET", f"{S}/results", "judge_a", 404),
    ("GET", f"{S}/results", "organizer", 200),
    ("GET", f"{S}/rubric", "participant", 403),
    ("GET", f"{S}/rubric", "judge_a", 200),
    ("GET", f"/e/{SLUG}/organizer/results", "judge_a", 403),
    ("GET", f"/e/{SLUG}/organizer/results", "organizer", 200),
    ("GET", f"/e/{SLUG}/judge", "participant", 403),
    ("GET", f"/e/{SLUG}/judge", "judge_a", 200),
    ("GET", f"/e/{SLUG}/judge/review/prj_01", "judge_b", 403),  # not assigned → refused
]


@pytest.mark.parametrize("method,path,role,expected", MATRIX)
def test_matrix(client, auth, method, path, role, expected):
    headers = auth(role) if role else {}
    r = client.request(method, path, headers=headers)
    assert r.status_code == expected, f"{role} {method} {path} → {r.status_code}, wanted {expected}"


def test_judge_reviews_never_leak_other_judges(client, auth):
    """Even the 200 responses only ever contain the requested judge's own reviews."""
    r = client.get(f"{S}/judges/me/reviews", headers=auth("judge_a")).json()
    assert r["judge"] == "jdg_24" and all(rv["judge"] == "jdg_24" for rv in r["reviews"])
    assert len(r["reviews"]) == 11
    r = client.get(f"{S}/judges/jdg_26/reviews", headers=auth("organizer")).json()
    assert all(rv["judge"] == "jdg_26" for rv in r["reviews"]) and len(r["reviews"]) == 10


def test_results_expose_judge_calibration_only_to_organizers(client, auth):
    assert client.get(f"{S}/results", headers=auth("organizer")).json()["judges"]
