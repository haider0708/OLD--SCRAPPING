from scraper.base import classify_http_status


def test_classify_http_status_retryable():
    retryable = {408, 409, 425, 429, 500, 502, 503, 504}
    for status_code in retryable:
        policy = classify_http_status(status_code)
        assert policy["retry"] is True


def test_classify_http_status_terminal():
    for status_code in (400, 401, 403, 404, 410, 422):
        policy = classify_http_status(status_code)
        assert policy["retry"] is False
