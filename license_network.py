# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
"""Restricted HTTPS JSON client backed by QGIS' network manager."""

import json
from urllib.parse import urlencode, urlparse

try:
    from qgis.PyQt.QtCore import QByteArray, QEventLoop, QTimer, QUrl
    from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest
    from qgis.core import QgsNetworkAccessManager
except ImportError:  # Enables syntax and offline checks outside QGIS.
    QByteArray = None
    QEventLoop = None
    QTimer = None
    QUrl = None
    QNetworkReply = None
    QNetworkRequest = None
    QgsNetworkAccessManager = None


ALLOWED_HOST = "aktivasi.ruangspasial.my.id"
MAX_RESPONSE_BYTES = 1024 * 1024


def is_allowed_url(url):
    """Return True only for the License Hub HTTPS origin."""
    try:
        parsed = urlparse(str(url))
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme.lower() == "https"
        and parsed.hostname == ALLOWED_HOST
        and port in (None, 443)
        and parsed.username is None
        and parsed.password is None
    )


def _enum_value(owner, group_name, value_name):
    value = getattr(owner, value_name, None)
    if value is not None:
        return value
    group = getattr(owner, group_name, None)
    return getattr(group, value_name, None) if group is not None else None


def _execute_event_loop(event_loop):
    if hasattr(event_loop, "exec"):
        return event_loop.exec()
    return event_loop.exec_()


def _decode_json(raw):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, (dict, list)) else None


def request_json(method, url, payload=None, timeout=15, user_agent="ZoneSculpt-QGIS"):
    """Send one JSON request and return ``(response, error_message)``."""
    method = str(method or "POST").upper()
    payload = dict(payload or {})
    if method not in ("GET", "POST"):
        return None, "Unsupported License Hub request method."
    if method == "GET" and payload:
        separator = "&" if "?" in url else "?"
        url = url + separator + urlencode(payload)
    if not is_allowed_url(url):
        return None, "The license request was blocked because its URL is not allowed."
    if QgsNetworkAccessManager is None:
        return None, "QGIS network manager is not available."

    request = QNetworkRequest(QUrl(url))
    request.setRawHeader(QByteArray(b"Accept"), QByteArray(b"application/json"))
    request.setRawHeader(
        QByteArray(b"User-Agent"),
        QByteArray(str(user_agent).encode("ascii", errors="ignore")),
    )

    redirect_attribute = _enum_value(
        QNetworkRequest, "Attribute", "RedirectPolicyAttribute"
    )
    manual_redirect = _enum_value(
        QNetworkRequest, "RedirectPolicy", "ManualRedirectPolicy"
    )
    if redirect_attribute is not None and manual_redirect is not None:
        request.setAttribute(redirect_attribute, manual_redirect)

    manager = QgsNetworkAccessManager.instance()
    if method == "GET":
        reply = manager.get(request)
    else:
        content_type_header = _enum_value(
            QNetworkRequest, "KnownHeaders", "ContentTypeHeader"
        )
        if content_type_header is not None:
            request.setHeader(content_type_header, "application/json")
        body = QByteArray(json.dumps(payload).encode("utf-8"))
        reply = manager.post(request, body)

    event_loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    reply.finished.connect(event_loop.quit)
    timer.timeout.connect(event_loop.quit)
    timer.start(max(1, int(float(timeout) * 1000)))
    _execute_event_loop(event_loop)

    if not reply.isFinished():
        reply.abort()
        reply.deleteLater()
        return None, "License Hub did not respond before the timeout."

    final_url = reply.url().toString()
    raw_bytes = bytes(reply.readAll())
    error_code = reply.error()
    error_text = reply.errorString()
    reply.deleteLater()

    if not is_allowed_url(final_url):
        return None, "License Hub redirected the request to an untrusted URL."
    if len(raw_bytes) > MAX_RESPONSE_BYTES:
        return None, "License Hub returned an unexpectedly large response."

    parsed = _decode_json(raw_bytes.decode("utf-8", errors="replace"))
    if parsed is not None:
        return parsed, None

    no_error = None
    if QNetworkReply is not None:
        no_error = _enum_value(QNetworkReply, "NetworkError", "NoError")
    if no_error is None or error_code != no_error:
        return None, "License Hub request failed: %s" % error_text
    return None, "License Hub returned an invalid JSON response."

