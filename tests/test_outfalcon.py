import json
from datetime import datetime, timezone

import httpx
import pytest

from tri_launch.outfalcon import OutFalconApiError, OutFalconClient
from tri_launch.outfalcon_adapter import (
    csv_import_mapping,
    leads_to_csv,
    outfalcon_variable,
    sanitize_variable,
    to_outfalcon_body,
    to_outfalcon_subject,
    to_outfalcon_template,
)


def test_spintax_and_variables_are_converted():
    text = (
        "{{RANDOM | Hi | Hey}} {{firstName | there}}, "
        "{{RANDOM | quick question | one question}} about {{companyName}} "
        "and {{category}}."
    )
    converted, warnings = to_outfalcon_template(text)
    assert "{Hi|Hey}" in converted
    assert "{{first_name}}" in converted
    assert "{{company}}" in converted
    assert "{{category}}" in converted  # custom variables pass through
    assert "{{RANDOM" not in converted
    assert warnings  # the dropped fallback is reported


def test_nested_variables_in_options():
    converted, _ = to_outfalcon_template(
        "{{RANDOM | your work at {{companyName}} | what {{companyName}} does}}"
    )
    assert converted == "{your work at {{company}}|what {{company}} does}"


def test_sender_and_signature_variables():
    subject, _ = to_outfalcon_subject("quick question, {{firstName | there}}")
    assert subject == "quick question, {{first_name}}"
    body, _ = to_outfalcon_body("Best,\n{{accountSignature}}")
    assert "{{sender_signature}}" in body
    assert body.startswith("<div>Best,<br />{{sender_signature}}</div>")


def test_instantly_date_scriptlet_becomes_concrete_date():
    """OutFalcon has no {# #} scriptlets - they must not reach the sent email."""
    text = (
        "P.S. If we don't deliver 20 meetings by "
        "{# 'now' | date: '%s' | plus: 2209600 | date: '%B %d, %Y' #}, you don't pay."
    )
    converted, _ = to_outfalcon_template(
        text, now=datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc)
    )
    assert "{#" not in converted
    assert converted == (
        "P.S. If we don't deliver 20 meetings by November 01, 2026, you don't pay."
    )


def test_date_scriptlet_without_plus_uses_today():
    converted, _ = to_outfalcon_template(
        "Offer valid until {# 'now' | date: '%B %d, %Y' #}.",
        now=datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc),
    )
    assert converted == "Offer valid until October 07, 2026."


def test_unknown_date_scriptlet_is_dropped_with_warning():
    converted, warnings = to_outfalcon_template("Until {# nonsense #}!")
    assert converted == "Until !"
    assert any("date tag" in w for w in warnings)


def test_csv_import_mapping_uses_explicit_variable_map():
    headers = ["Email", "First Name", "Company", "category", "companyShort"]
    mapping = csv_import_mapping(
        headers,
        {"firstName": "First Name", "companyName": "Company", "companyShort": "companyShort"},
    )
    assert mapping["Email"] == "email"
    assert mapping["First Name"] == "first_name"
    assert mapping["Company"] == "company"
    assert mapping["category"] == "custom:category"
    assert mapping["companyShort"] == "custom:companyShort"


def test_leads_to_csv_shape():
    csv_text, mapping = leads_to_csv(
        [
            {
                "email": "a@x.com",
                "first_name": "Ada",
                "company_name": "Acme",
                "custom_variables": {"category": "fintech", "list_name": "Alpha"},
            }
        ]
    )
    assert "email,first_name,company_name,category,list_name" in csv_text
    assert "Ada" in csv_text and "fintech" in csv_text
    assert mapping["company_name"] == "company"
    assert mapping["category"] == "custom:category"


def test_variable_names_are_mapped_case_insensitively_and_sanitized():
    assert outfalcon_variable("CompanyName") == "company"
    assert outfalcon_variable("COMPANYNAME") == "company"
    assert outfalcon_variable("FirstName") == "first_name"
    assert outfalcon_variable("AccountSignature") == "sender_signature"
    assert outfalcon_variable("software type") == "software_type"
    assert outfalcon_variable("ideal buyers") == "ideal_buyers"
    assert outfalcon_variable("software-type!") == "software_type"
    assert sanitize_variable("2fast") == "v_2fast"
    assert sanitize_variable("   ") == "variable"


def test_templates_get_outfalcon_safe_tags():
    converted, _ = to_outfalcon_template(
        "{{software type}} and {{ideal buyers}} at {{CompanyName}} ({{firstName}})"
    )
    assert "{{software_type}}" in converted
    assert "{{ideal_buyers}}" in converted
    assert "{{company}}" in converted
    assert "{{first_name}}" in converted
    assert "{{software type}}" not in converted


def test_leads_to_csv_sanitizes_custom_keys_consistently():
    csv_text, mapping = leads_to_csv(
        [
            {
                "email": "a@x.com",
                "company_name": "Acme",
                "custom_variables": {
                    "software type": "AI lead response software",
                    "ideal buyers": "sales teams",
                },
            }
        ]
    )
    assert "software_type" in csv_text.splitlines()[0]
    assert "ideal_buyers" in csv_text.splitlines()[0]
    assert "AI lead response software" in csv_text
    assert mapping["software_type"] == "custom:software_type"
    assert mapping["ideal_buyers"] == "custom:ideal_buyers"
    assert "software type" not in mapping


def test_client_unwraps_envelope_and_sends_api_key():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "mk_live_test"
        return httpx.Response(200, json={"data": [{"id": "c1", "name": "Settings"}], "meta": {}})

    client = OutFalconClient("mk_live_test", transport=httpx.MockTransport(handler))
    campaigns = client.list_campaigns()
    assert campaigns == [{"id": "c1", "name": "Settings"}]
    client.close()


def test_client_raises_with_error_envelope():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": {"message": "name is required", "code": "bad_request"}}
        )

    client = OutFalconClient("mk_live_test", transport=httpx.MockTransport(handler))
    with pytest.raises(OutFalconApiError) as exc:
        client.create_campaign("")
    assert exc.value.status == 400
    client.close()
