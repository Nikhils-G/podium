"""One event's whole life through the web console, the way an organizer, a team and a judge click
through it: create the event and its rubric, a team submits, a judge is invited, assigned and
scores, and the organizer closes judging and publishes the results."""

import re
from datetime import UTC, datetime, timedelta

from tests.test_forms_sweep import demo


def test_create_submit_judge_publish_through_the_console(app):
    org = demo(app, "organizer")
    close = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    r = org.post(
        "/events/new",
        {
            "name": "Lifecycle Night",
            "is_public": "on",
            "submissions_close_at": close,
            "max_team_size": "4",
        },
    )
    assert r.status_code == 303 and r.headers["location"].endswith("?saved=created"), r.text[:300]
    slug = r.headers["location"].split("/")[2]
    base = f"/e/{slug}/organizer"
    for name, weight in (("Impact", "2"), ("Execution", "1")):
        r = org.post(
            f"{base}/rubric", {"name": name, "weight": weight, "min_score": "1", "max_score": "5"}
        )
        assert r.status_code == 303, r.text[:300]

    team = demo(app, "participant")
    assert team.post(f"/e/{slug}/team", {"name": "Night Owls"}).status_code == 303
    r = team.post(
        f"/e/{slug}/submit",
        {
            "action": "submit",
            "title": "Lantern",
            "summary": "A small lamp that reminds your team to take breaks.",
            "repo_url": "https://github.com/example/lantern",
        },
    )
    assert r.status_code == 303, r.text[:300]
    assert "Lantern" in team.get(f"/e/{slug}/projects").text, "the project is in the gallery"

    r = org.post(f"{base}/judges/invite", {"email": "diego.herrera@example.org"})
    assert r.status_code == 200
    link = re.search(r"/judge-invite/[A-Za-z0-9_-]+", r.text).group(0)
    judge = demo(app, "judge_a")
    r = judge.post(link)
    assert r.status_code == 303 and r.headers["location"].endswith("?saved=joined")

    r = org.post(f"{base}/assignments/auto/apply", {"reviews_per_project": "3", "seed": "1"})
    assert r.status_code in (200, 303), r.text[:300]
    assert (
        org.post(f"{base}/dates", {"field": "submissions_close_at", "preset": "now"}).status_code
        == 303
    )
    assert org.post(f"{base}/actions/open_judging").status_code == 303

    queue = judge.get(f"/e/{slug}/judge").text
    pid = re.search(rf"/e/{slug}/judge/review/(prj_[A-Za-z0-9]+)", queue).group(1)
    page = judge.get(f"/e/{slug}/judge/review/{pid}").text
    names = sorted(set(re.findall(r'name="(crt_[A-Za-z0-9]+)"', page)))
    assert len(names) == 2, "both criteria appear on the review form"
    r = judge.post(
        f"/e/{slug}/judge/review/{pid}", {"action": "submit", names[0]: "4", names[1]: "5"}
    )
    assert r.status_code == 303 and "saved=submitted" in r.headers["location"]

    for action in ("close_judging", "publish_results"):
        r = org.post(f"{base}/actions/{action}")
        assert r.status_code == 303 and r.headers["location"].endswith(f"?saved={action}"), action
    results = demo(app, "participant").get(f"/e/{slug}/results")
    assert results.status_code == 200 and "Lantern" in results.text
