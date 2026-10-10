from calendar_sync.mapping import (
    extract_client,
    extract_client_name_from_title,
    extract_meet_url,
    matches_title_prefix,
    parse_expected_languages,
)


def test_extract_meet_url_from_hangout_link():
    event = {"hangoutLink": "https://meet.google.com/abc-defg-hij"}
    assert extract_meet_url(event) == "https://meet.google.com/abc-defg-hij"


def test_extract_meet_url_from_conference_data_entry_points():
    event = {
        "conferenceData": {
            "entryPoints": [
                {"entryPointType": "phone", "uri": "tel:+1-555-0100"},
                {"entryPointType": "video", "uri": "https://meet.google.com/xyz-wxyz-abc"},
            ]
        }
    }
    assert extract_meet_url(event) == "https://meet.google.com/xyz-wxyz-abc"


def test_extract_meet_url_prefers_hangout_link_over_entry_points():
    event = {
        "hangoutLink": "https://meet.google.com/abc-defg-hij",
        "conferenceData": {
            "entryPoints": [{"entryPointType": "video", "uri": "https://meet.google.com/other"}]
        },
    }
    assert extract_meet_url(event) == "https://meet.google.com/abc-defg-hij"


def test_extract_meet_url_none_when_missing():
    assert extract_meet_url({}) is None
    assert extract_meet_url({"conferenceData": {"entryPoints": []}}) is None


def test_parse_expected_languages_basic():
    assert parse_expected_languages("Agenda\nLanguage: hi, en\nmore text") == ["hi", "en"]


def test_parse_expected_languages_case_insensitive_and_spacing():
    assert parse_expected_languages("LANGUAGE:   Hi ,  En  ") == ["hi", "en"]


def test_parse_expected_languages_missing():
    assert parse_expected_languages("just a normal description") is None
    assert parse_expected_languages(None) is None


def test_matches_title_prefix():
    event = {"summary": "Client Call: Acme Corp"}
    assert matches_title_prefix(event, "Client Call:") is True
    assert matches_title_prefix(event, "Internal:") is False
    assert matches_title_prefix(event, None) is True
    assert matches_title_prefix({}, "Client Call:") is False


def test_extract_client_name_from_title():
    assert extract_client_name_from_title("30 min with Information (Acme Corp)") == "Acme Corp"
    assert extract_client_name_from_title("30 min with Information") is None
    assert extract_client_name_from_title(None) is None


def test_extract_client_prefers_title_name_over_display_name():
    event = {
        "summary": "30 min with Information (Acme Corp)",
        "attendees": [
            {"email": "it@fincelerate.com"},
            {"email": "jane@acme.com", "displayName": "Jane Doe"},
        ],
    }
    client = extract_client(event, "it@fincelerate.com")
    assert client == {"name": "Acme Corp", "email": "jane@acme.com"}


def test_extract_client_falls_back_to_display_name():
    event = {
        "summary": "30 min with Information",
        "attendees": [
            {"email": "it@fincelerate.com"},
            {"email": "jane@acme.com", "displayName": "Jane Doe"},
        ],
    }
    client = extract_client(event, "it@fincelerate.com")
    assert client == {"name": "Jane Doe", "email": "jane@acme.com"}


def test_extract_client_excludes_organizer_and_bot_accounts():
    event = {
        "summary": "30 min with Information (Acme Corp)",
        "attendees": [
            {"email": "it@fincelerate.com"},
            {"email": "fincelerate.notetaker1@gmail.com"},
            {"email": "jane@acme.com", "displayName": "Jane Doe"},
        ],
    }
    client = extract_client(event, "it@fincelerate.com", excluded_emails=["fincelerate.notetaker1@gmail.com"])
    assert client == {"name": "Acme Corp", "email": "jane@acme.com"}


def test_extract_client_none_when_no_other_attendees():
    event = {"summary": "Internal sync", "attendees": [{"email": "it@fincelerate.com"}]}
    assert extract_client(event, "it@fincelerate.com") is None
    assert extract_client({}, "it@fincelerate.com") is None
