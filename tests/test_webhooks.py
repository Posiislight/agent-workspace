import hashlib
import hmac as hmac_mod

from app.github.webhooks import classify_event, parse_comment, verify_signature

SECRET = "whsec"
BODY = b'{"action": "submitted"}'


def _sig(body, secret=SECRET):
    return "sha256=" + hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_verify_signature_accepts_valid():
    assert verify_signature(SECRET, BODY, _sig(BODY)) is True


def test_verify_signature_rejects_bad_and_missing():
    assert verify_signature(SECRET, BODY, "sha256=" + "0" * 64) is False
    assert verify_signature(SECRET, BODY, None) is False
    assert verify_signature(SECRET, BODY, "sha1=abc") is False
    assert verify_signature("", BODY, _sig(BODY)) is False


def test_parse_comment():
    assert parse_comment("approve") == ("approved", "")
    assert parse_comment("Approve please") == ("approved", "")
    assert parse_comment("reject: the plan missed edge cases") == (
        "rejected", "the plan missed edge cases")
    assert parse_comment("Reject") == ("rejected", "")
    assert parse_comment("LGTM ship it") is None
    assert parse_comment("") is None


def test_classify_review_approved():
    payload = {"action": "submitted",
               "pull_request": {"number": 9},
               "review": {"state": "approved", "body": "nice"}}
    assert classify_event("pull_request_review", payload) == {
        "pr_number": 9, "decision": "approved", "feedback": "nice"}


def test_classify_changes_requested():
    payload = {"action": "submitted",
               "pull_request": {"number": 9},
               "review": {"state": "changes_requested", "body": "redo it"}}
    assert classify_event("pull_request_review", payload) == {
        "pr_number": 9, "decision": "rejected", "feedback": "redo it"}


def test_classify_ready_for_review():
    payload = {"action": "ready_for_review", "pull_request": {"number": 4}}
    assert classify_event("pull_request", payload) == {
        "pr_number": 4, "decision": "approved", "feedback": ""}


def test_classify_comment_command_ignores_bots_and_plain_comments():
    base = {"action": "created",
            "issue": {"number": 9, "pull_request": {"url": "x"}},
            "comment": {"body": "approve", "user": {"type": "User"}}}
    assert classify_event("issue_comment", base) == {
        "pr_number": 9, "decision": "approved", "feedback": ""}
    bot = {**base, "comment": {"body": "approve", "user": {"type": "Bot"}}}
    assert classify_event("issue_comment", bot) is None
    plain = {**base, "comment": {"body": "looks good", "user": {"type": "User"}}}
    assert classify_event("issue_comment", plain) is None
    issue_only = {"action": "created",
                  "issue": {"number": 9}, "comment": {"body": "approve",
                                                      "user": {"type": "User"}}}
    assert classify_event("issue_comment", issue_only) is None


def test_classify_ignores_other_events():
    assert classify_event("push", {"action": "x"}) is None
    assert classify_event("pull_request", {"action": "opened"}) is None