from urllib.parse import urlencode

from operator_ai import channels, settings, tenancy, vault


def _post(client, params, token=None, sig=None):
    url = "http://testserver/t/owner/hooks/voice"
    s = sig if sig is not None else channels.twilio_signature(token, url, params) if token else ""
    return client.post("/t/owner/hooks/voice", content=urlencode(params),
                       headers={"content-type": "application/x-www-form-urlencoded", "x-twilio-signature": s})


def test_voice_line_off_by_default(client):
    r = _post(client, {"From": "+1-555-0142"})
    assert r.status_code == 200 and "not switched on" in r.text


def test_voice_line_answers_opted_in_caller(client):
    t = tenancy.get("owner")
    vault.put(t, {"sms_auth_token": "voicetok"})
    settings.set_box(t, "live_channels_enabled", True, "test")
    assert _post(client, {"From": "+1-555-0142"}, sig="bad").status_code == 401
    r = _post(client, {"From": "+1-555-0142"}, token="voicetok")
    assert "isn't registered" in r.text
    with t.db() as c:
        c.execute("INSERT INTO channel_links(channel,ext_id,opted_in,status,created) VALUES('phone','+1-555-0142',1,'opted_in',0)")
    r = _post(client, {"From": "+1-555-0142"}, token="voicetok")
    assert "<Gather" in r.text
    r = _post(client, {"From": "+1-555-0142", "SpeechResult": "What are the rules for a Thunder Dome test?"}, token="voicetok")
    assert r.headers["content-type"].startswith("application/xml")
    assert "three and five creatives" in r.text and "[S1]" not in r.text and "Polly.Joanna" in r.text
