def test_create_meeting(client):
    resp = client.post(
        "/meetings",
        json={
            "meet_url": "https://meet.google.com/abc-defg-hij",
            "scheduled_at": "2026-10-11T10:00:00Z",
            "expected_languages": ["hi", "en"],
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "scheduled"
    assert body["expected_languages"] == ["hi", "en"]
    assert body["audio_key"] is None


def test_create_meeting_rejects_non_meet_url(client):
    resp = client.post(
        "/meetings",
        json={"meet_url": "https://zoom.us/j/123", "scheduled_at": "2026-10-11T10:00:00Z"},
    )
    assert resp.status_code == 422


def test_get_meeting(client):
    created = client.post(
        "/meetings",
        json={"meet_url": "https://meet.google.com/abc-defg-hij", "scheduled_at": "2026-10-11T10:00:00Z"},
    ).json()

    resp = client.get(f"/meetings/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]


def test_get_meeting_not_found(client):
    resp = client.get("/meetings/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


def test_list_meetings(client):
    for _ in range(3):
        client.post(
            "/meetings",
            json={"meet_url": "https://meet.google.com/abc-defg-hij", "scheduled_at": "2026-10-11T10:00:00Z"},
        )

    resp = client.get("/meetings")
    assert resp.status_code == 200
    assert len(resp.json()) == 3


def test_list_meetings_filter_by_status(client):
    client.post(
        "/meetings",
        json={"meet_url": "https://meet.google.com/abc-defg-hij", "scheduled_at": "2026-10-11T10:00:00Z"},
    )

    resp = client.get("/meetings", params={"status": "scheduled"})
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    resp = client.get("/meetings", params={"status": "done"})
    assert resp.status_code == 200
    assert len(resp.json()) == 0
