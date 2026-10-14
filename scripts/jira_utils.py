"""Shared Jira API, ADF conversion, and content processing utilities.

Used by both submit.py (standard) and split_submit.py (split submissions).

Environment variables:
    JIRA_SERVER  Jira server URL (e.g. https://mysite.atlassian.net)
    JIRA_USER    Jira username/email
    JIRA_TOKEN   Jira API token
"""

import base64
import collections
import hashlib
import json
import os
import re
import ssl
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request

from strat_side_effects import check_owner, digest, perform

ssl_ctx = ssl.create_default_context()
try:
    import certifi
    ssl_ctx.load_verify_locations(certifi.where())
except (ImportError, OSError):
    pass


# ─── HTTP Layer ───────────────────────────────────────────────────────────────

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Disable urllib's implicit redirect following.

    Forces redirects to surface as HTTPError so make_request can apply
    _allowed_redirect validation before forwarding credentials.
    """
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_redirect_opener = urllib.request.build_opener(
    _NoRedirect(),
    urllib.request.HTTPSHandler(context=ssl_ctx),
)

_MAX_REDIRECTS = 5


def _allowed_redirect(original_url, redirect_url):
    """Validate that redirect_url is safe to follow with credentials.

    Allows same-origin and *.atlassian.net (Jira Cloud host migration).
    """
    from urllib.parse import urljoin, urlparse
    redirect_resolved = urljoin(original_url, redirect_url)
    orig = urlparse(original_url)
    redir = urlparse(redirect_resolved)
    if redir.scheme != "https":
        return False, redirect_resolved
    orig_host = orig.netloc.lower()
    redir_host = redir.netloc.lower()
    if orig_host == redir_host:
        return True, redirect_resolved
    if redir_host.endswith(".atlassian.net"):
        return True, redirect_resolved
    return False, redirect_resolved


def make_request(url, user, token, body=None, method=None,
                 _redirect_count=0):
    """HTTP request with Basic Auth. Returns parsed JSON or None for 204."""
    credentials = base64.b64encode(f"{user}:{token}".encode()).decode()
    headers = {
        "Authorization": f"Basic {credentials}",
        "Accept": "application/json",
    }
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _redirect_opener.open(req, timeout=60) as resp:
            if resp.status == 204:
                return None
            resp_body = resp.read()
            if not resp_body:
                return None
            return json.loads(resp_body)
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            redirect_url = e.headers.get("Location")
            if redirect_url:
                if _redirect_count >= _MAX_REDIRECTS:
                    e.close()
                    raise RuntimeError(
                        f"Too many redirects (>{_MAX_REDIRECTS})")
                same, resolved = _allowed_redirect(url, redirect_url)
                if not same:
                    e.close()
                    raise RuntimeError(
                        f"Redirect to disallowed origin: {resolved}")
                e.close()
                return make_request(resolved, user, token, body, method,
                                    _redirect_count=_redirect_count + 1)
        raise


def api_call(server, path, user, token, body=None, method=None):
    """Build full URL and call make_request."""
    url = f"{server.rstrip('/')}/rest/api/3{path}"
    return make_request(url, user, token, body, method)


def api_call_with_retry(server, path, user, token, body=None, method=None,
                        max_retries=3):
    """Retry reads only; writes must reconcile a possibly committed request.

    POST search is a read. Even a transient write error can arrive after Jira
    commits, so an HTTP retry must not silently repeat that mutation.
    """
    actual_method = method or ("POST" if body is not None else "GET")
    if actual_method not in ("GET", "HEAD") and path != "/search/jql":
        return api_call(server, path, user, token, body, method)
    last_error = None
    for attempt in range(max_retries):
        try:
            return api_call(server, path, user, token, body, method)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = int(e.headers.get("Retry-After", 1))
                wait = max(retry_after, 1)
                print(f"  Rate limited, waiting {wait}s...", file=sys.stderr)
                time.sleep(wait)
                last_error = e
                continue
            if e.code in (502, 503, 504):
                wait = 4 ** attempt  # 1, 4, 16
                print(f"  HTTP {e.code}, retrying in {wait}s...",
                      file=sys.stderr)
                time.sleep(wait)
                last_error = e
                continue
            error_body = e.read().decode("utf-8", errors="replace")
            print(f"HTTP {e.code}: {error_body}", file=sys.stderr)
            e.error_body = error_body
            raise
        except urllib.error.URLError as e:
            wait = 4 ** attempt
            print(f"  Network error: {e.reason}, retrying in {wait}s...",
                  file=sys.stderr)
            time.sleep(wait)
            last_error = e
    raise last_error


def require_env():
    """Read and validate Jira env vars. Returns (server, user, token)."""
    server = os.environ.get("JIRA_SERVER")
    user = os.environ.get("JIRA_USER")
    token = os.environ.get("JIRA_TOKEN")
    return server, user, token


# ─── Jira Operations ─────────────────────────────────────────────────────────

def search_issues(server, user, token, jql, fields=None, max_results=50):
    """Search Jira issues using JQL via /rest/api/3/search/jql.

    Uses nextPageToken pagination (the startAt-based /search endpoint
    was removed by Atlassian in 2026).
    """
    if fields is None:
        fields = ["key"]
    all_issues = []
    next_page_token = None
    while True:
        body = {
            "jql": jql,
            "maxResults": min(max_results, 100),
            "fields": fields,
        }
        if next_page_token:
            body["nextPageToken"] = next_page_token
        data = api_call_with_retry(server, "/search/jql", user, token,
                                   body=body)
        issues = data.get("issues", [])
        all_issues.extend(issues)
        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break
    return all_issues


def query_label_counts(server, user, token, labels):
    """Count Jira issues per label. Returns {label: count} dict."""
    counts = {}
    for label in labels:
        jql = f'labels = "{label}"'
        issues = search_issues(server, user, token, jql, fields=["key"])
        counts[label] = len(issues)
    return counts


def build_jql_from_config(config_path):
    """Build a JQL query string from pipeline-settings.yaml."""
    import yaml
    with open(config_path) as f:
        cfg = yaml.safe_load(f)
    jql_cfg = cfg.get("jql", {})
    project = jql_cfg.get("project", "RHAIRFE")
    required = jql_cfg.get("required_labels", [])
    target_versions = jql_cfg.get("target_versions", [])
    quality = jql_cfg.get("quality_labels", [])
    excluded_labels = jql_cfg.get("excluded_labels", [])
    excluded = jql_cfg.get("excluded_statuses", [])
    order = jql_cfg.get("order_by", "key ASC")

    clauses = [f'project = {project}']
    label_clause = " OR ".join(f'labels = "{label}"' for label in required)
    version_clause = ""
    if target_versions:
        versions_csv = ", ".join(f'"{v}"' for v in target_versions)
        version_clause = f'cf[10855] in ({versions_csv})'
    if label_clause and version_clause:
        clauses.append(f'({label_clause} OR {version_clause})')
    elif label_clause:
        if len(required) > 1:
            clauses.append(f'({label_clause})')
        else:
            clauses.append(label_clause)
    elif version_clause:
        clauses.append(version_clause)
    if quality:
        quality_clause = " OR ".join(f'labels = "{label}"' for label in quality)
        clauses.append(f'({quality_clause})')
    if excluded_labels:
        labels_csv = ", ".join(f'"{label}"' for label in excluded_labels)
        clauses.append(f'(labels NOT IN ({labels_csv}) OR labels IS EMPTY)')
    if excluded:
        status_clause = ", ".join(f'"{s}"' for s in excluded)
        clauses.append(f'status NOT IN ({status_clause})')

    jql = " AND ".join(clauses)
    if order:
        jql += f" ORDER BY {order}"
    return jql


def _extract_rfe_keys_from_issues(issues):
    """Extract RHAIRFE keys from Cloners links on a list of RHAISTRAT issues."""
    rfe_keys = set()
    for issue in issues:
        links = issue.get("fields", {}).get("issuelinks", [])
        for link in links:
            if link.get("type", {}).get("name") != "Cloners":
                continue
            outward = link.get("outwardIssue", {})
            inward = link.get("inwardIssue", {})
            rfe_key = None
            if outward and outward.get("key", "").startswith("RHAIRFE"):
                rfe_key = outward["key"]
            elif inward and inward.get("key", "").startswith("RHAIRFE"):
                rfe_key = inward["key"]
            if rfe_key:
                rfe_keys.add(rfe_key)
    return rfe_keys


def _count_rfe_clones_from_issues(issues):
    """Count RHAISTRAT clones per RHAIRFE key from Cloners links."""
    counts = collections.Counter()
    for issue in issues:
        links = issue.get("fields", {}).get("issuelinks", [])
        for link in links:
            if link.get("type", {}).get("name") != "Cloners":
                continue
            outward = link.get("outwardIssue", {})
            inward = link.get("inwardIssue", {})
            rfe_key = None
            if outward and outward.get("key", "").startswith("RHAIRFE"):
                rfe_key = outward["key"]
            elif inward and inward.get("key", "").startswith("RHAIRFE"):
                rfe_key = inward["key"]
            if rfe_key:
                counts[rfe_key] += 1
    return counts


def find_processed_rfe_ids(server, user, token, skip_labels,
                           excluded_strat_statuses=None,
                           strat_project="RHAISTRAT"):
    """Find RHAIRFE IDs that should be excluded from batching.

    Excludes RFEs whose RHAISTRAT clones either:
    - Have any of the skip labels (already processed by pipeline)
    - Are in an active/completed status (being worked on or done)

    Override: if an excluded RFE has exactly one open RHAISTRAT clone
    that has no skip labels and is not in an excluded status, the RFE
    is un-excluded so the replacement clone can be processed. This
    handles the case where an old clone was closed/rejected and a new
    one was created as a replacement.
    """
    processed = set()

    if skip_labels:
        label_clause = " OR ".join(f'labels = "{label}"' for label in skip_labels)
        jql = f"project = {strat_project} AND ({label_clause})"
        issues = search_issues(server, user, token, jql,
                               fields=["issuelinks"])
        processed |= _extract_rfe_keys_from_issues(issues)

    if excluded_strat_statuses:
        status_clause = ", ".join(f'"{s}"' for s in excluded_strat_statuses)
        jql = f"project = {strat_project} AND status IN ({status_clause})"
        issues = search_issues(server, user, token, jql,
                               fields=["issuelinks"])
        processed |= _extract_rfe_keys_from_issues(issues)

    if processed and excluded_strat_statuses:
        sc = ", ".join(f'"{s}"' for s in excluded_strat_statuses)
        open_jql = (f"project = {strat_project} "
                    f"AND status NOT IN ({sc})")
        open_issues = search_issues(server, user, token, open_jql,
                                    fields=["issuelinks", "labels"])
        open_counts = _count_rfe_clones_from_issues(open_issues)

        unlabeled_rfes = set()
        if skip_labels:
            skip_set = set(skip_labels)
            truly_unlabeled = [
                issue for issue in open_issues
                if not (set(
                    label.get("name", label) if isinstance(label, dict) else label
                    for label in issue.get("fields", {}).get("labels", [])
                ) & skip_set)
            ]
            unlabeled_rfes = _extract_rfe_keys_from_issues(
                truly_unlabeled)
        else:
            unlabeled_rfes = set(open_counts)

        for rfe_key in unlabeled_rfes:
            if open_counts.get(rfe_key) == 1 and rfe_key in processed:
                processed.discard(rfe_key)

    return processed


def get_project_versions(server, user, token, project_key):
    """Return versions for a project, as a list of {"id", "name", ...} dicts."""
    data = api_call_with_retry(server, f"/project/{project_key}", user, token)
    return data.get("versions", [])


def get_issue(server, user, token, key, fields=None):
    """GET /rest/api/3/issue/{key}"""
    path = f"/issue/{key}"
    if fields:
        path += f"?fields={','.join(fields)}"
    return api_call_with_retry(server, path, user, token)


def get_comments(server, user, token, issue_key):
    """GET all comments for an issue, handling pagination."""
    comments = []
    start_at = 0
    while True:
        path = f"/issue/{issue_key}/comment?startAt={start_at}&maxResults=100"
        data = api_call_with_retry(server, path, user, token)
        batch = data.get("comments", [])
        comments.extend(batch)
        if start_at + len(batch) >= data.get("total", 0):
            break
        start_at += len(batch)
    return comments


def add_comment(server, user, token, issue_key, body_adf):
    """Post once per exact body, reconciling visible comments before retry."""
    path = f"/issue/{issue_key}/comment"
    def read():
        matches = [c for c in get_comments(server, user, token, issue_key) if c.get("body") == body_adf]
        if len(matches) > 1:
            raise RuntimeError("duplicate matching review comments require reconciliation")
        return matches[0] if matches else None
    return perform(server, "comment", issue_key, body_adf, read,
                   lambda: api_call(server, path, user, token, body={"body": body_adf}),
                   lambda data: check_owner(server, user, token, issue_key, data), slot="review-summary")


def create_clone(server, user, token, source_key, **fields):
    """Create with an atomic searchable marker; recover even before linking.

    A missing search result after pending intent is uncertain, not permission
    to create again. Both marker and Cloners links must resolve uniquely.
    """
    marker = "strat-creator-clone-" + digest({"server": server.rstrip("/"), "source": source_key,
                                           "project": fields["project"]})[:32]
    fields = dict(fields)
    fields["labels"] = sorted(set(fields.get("labels", [])) | {marker})
    def read():
        linked = get_issue(server, user, token, source_key, fields=["issuelinks"])["fields"].get("issuelinks", [])
        keys = {link.get(side, {}).get("key", "") for link in linked
                if link.get("type", {}).get("name") == "Cloners" for side in ("inwardIssue", "outwardIssue")
                if link.get(side, {}).get("key", "").startswith(fields["project"] + "-")}
        issues = search_issues(server, user, token, f'project = "{fields["project"]}" AND labels = "{marker}"')
        keys.update(issue["key"] for issue in issues)
        if len(keys) > 1:
            raise RuntimeError("multiple clone candidates require reconciliation")
        return next(iter(keys)) if keys else None
    return perform(server, "clone", source_key, fields, read,
                   lambda: create_issue(server, user, token, **fields),
                   lambda data: check_owner(server, user, token, source_key, data), slot=fields["project"])


def create_issue(server, user, token, project, issue_type, summary,
                 description_adf, priority, labels=None, components=None,
                 fix_versions=None, affects_versions=None, target_versions=None,
                 parent_key=None):
    """POST /rest/api/3/issue — returns the created issue key."""
    body = {
        "fields": {
            "project": {"key": project},
            "issuetype": {"name": issue_type},
            "summary": summary,
            "description": description_adf,
            "priority": {"name": priority},
            "assignee": None,
        }
    }
    if labels:
        body["fields"]["labels"] = labels
    if components:
        body["fields"]["components"] = [{"name": c} for c in components]
    if fix_versions:
        body["fields"]["fixVersions"] = [{"name": v} for v in fix_versions]
    if affects_versions:
        body["fields"]["versions"] = [{"name": v} for v in affects_versions]
    if target_versions:
        # Accept version references as dicts ({"id": ...} / {"name": ...}) or
        # plain name strings.
        body["fields"]["customfield_10855"] = [
            v if isinstance(v, dict) else {"name": v} for v in target_versions]
    if parent_key:
        body["fields"]["parent"] = {"key": parent_key}
    result = api_call_with_retry(server, "/issue", user, token, body=body)
    return result["key"]


def update_issue(server, user, token, issue_key, summary, description_adf):
    """PUT to update an existing issue's summary and description."""
    set_fields_verified(server, user, token, issue_key, {"summary": summary, "description": description_adf})


def update_summary(server, user, token, issue_key, summary):
    """PUT to update only an issue's summary field."""
    set_fields_verified(server, user, token, issue_key, {"summary": summary})


def add_labels(server, user, token, issue_key, labels):
    """Add labels to an existing issue without removing existing ones."""
    body = {
        "update": {
            "labels": [{"add": label} for label in labels]
        }
    }
    path = f"/issue/{issue_key}"
    _label_write(server, user, token, issue_key, labels, body, path, adding=True)


def remove_labels(server, user, token, issue_key, labels):
    """Remove labels from an existing issue without affecting other labels."""
    body = {
        "update": {
            "labels": [{"remove": label} for label in labels]
        }
    }
    path = f"/issue/{issue_key}"
    _label_write(server, user, token, issue_key, labels, body, path, adding=False)


def _label_write(server, user, token, key, labels, body, path, adding):
    def read():
        current = set(get_issue(server, user, token, key, fields=["labels"])["fields"].get("labels", []))
        done = set(labels) <= current if adding else not set(labels) & current
        return True if done else None
    perform(server, "add-labels" if adding else "remove-labels", key, sorted(labels), read,
            lambda: api_call(server, path, user, token, body=body, method="PUT"),
            lambda data: check_owner(server, user, token, key, data))


def set_fields_verified(server, user, token, key, fields):
    """Reconcile field replacement, halting if a pending write was overwritten."""
    def read():
        current = get_issue(server, user, token, key, fields=list(fields))["fields"]
        return True if all(current.get(k) == v for k, v in fields.items()) else None
    return perform(server, "fields", key, fields, read,
                   lambda: api_call(server, f"/issue/{key}", user, token, body={"fields": fields}, method="PUT"),
                   lambda data: check_owner(server, user, token, key, data))


def create_issue_link(server, user, token, type_name, inward_key, outward_key):
    """POST /rest/api/3/issueLink"""
    body = {
        "type": {"name": type_name},
        "inwardIssue": {"key": inward_key},
        "outwardIssue": {"key": outward_key},
    }
    def read():
        links = get_issue(server, user, token, inward_key, fields=["issuelinks"])["fields"].get("issuelinks", [])
        matches = [x for x in links if x.get("type", {}).get("name") == type_name
                   and x.get("outwardIssue", {}).get("key") == outward_key]
        if len(matches) > 1:
            raise RuntimeError("duplicate matching links require reconciliation")
        return True if matches else None
    # Cloners is created before the target has a source link; check the RFE.
    target = outward_key if outward_key.startswith("RHAIRFE-") else inward_key
    perform(server, "link", inward_key, body, read,
            lambda: api_call(server, "/issueLink", user, token, body=body),
            lambda data: check_owner(server, user, token, target, data))


def add_attachment(server, user, token, issue_key, filepath, filename=None,
                   max_retries=3):
    """Upload once per filename and exact bytes; never blindly retry POST."""
    import mimetypes
    if filename is None:
        filename = os.path.basename(filepath)
    boundary = f"----JiraAttachment{int(time.time() * 1000)}"
    with open(filepath, "rb") as f:
        file_data = f.read()
    mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode() + file_data + f"\r\n--{boundary}--\r\n".encode()

    url = f"{server.rstrip('/')}/rest/api/3/issue/{issue_key}/attachments"
    credentials = base64.b64encode(f"{user}:{token}".encode()).decode()
    headers = {
        "Authorization": f"Basic {credentials}",
        "Accept": "application/json",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "X-Atlassian-Token": "no-check",
    }
    def read():
        attachments = get_issue(server, user, token, issue_key, fields=["attachment"])["fields"].get("attachment", [])
        matches = []
        for att in attachments:
            if att.get("filename") != filename:
                continue
            with tempfile.TemporaryDirectory() as temp:
                local = os.path.join(temp, "attachment")
                download_attachment(server, user, token, att["content"], local)
                with open(local, "rb") as stream:
                    if stream.read() == file_data:
                        matches.append(att)
        if len(matches) > 1:
            raise RuntimeError("duplicate matching attachments require reconciliation")
        return matches if matches else None
    def write():
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=120, context=ssl_ctx) as resp:
            return json.loads(resp.read())
    return perform(server, "attachment", issue_key,
                   {"filename": filename, "sha256": hashlib.sha256(file_data).hexdigest()}, read, write,
                   lambda data: check_owner(server, user, token, issue_key, data), slot=filename)


def download_attachment(server, user, token, content_url, dest_path):
    """Download a Jira attachment by its content URL."""
    credentials = base64.b64encode(f"{user}:{token}".encode()).decode()
    headers = {
        "Authorization": f"Basic {credentials}",
        "Accept": "*/*",
    }
    req = urllib.request.Request(content_url, headers=headers)
    with urllib.request.urlopen(req, timeout=120, context=ssl_ctx) as resp:
        with open(dest_path, "wb") as f:
            f.write(resp.read())


def attachment_sort_key(attachment):
    """Return a deterministic key for selecting the newest attachment.

    Callers should use ``max(..., key=attachment_sort_key)``. Jira's
    ``created`` timestamp is authoritative; attachment ID breaks ties, with
    the content URL as a final stable tie-breaker.
    """
    created = str(attachment.get("created") or "")
    raw_id = str(attachment.get("id") or "")
    try:
        id_key = (1, int(raw_id))
    except ValueError:
        id_key = (0, raw_id)
    content_url = str(attachment.get("content") or "")
    return created, id_key, content_url


def delete_attachment(server, user, token, attachment_id, max_retries=3):
    """Delete a Jira attachment.

    Deprecated: the strat-creator pipeline intentionally no longer deletes
    attachments. CI and human-run flows may use credentials without
    attachment-delete permission, so callers should use append-only uploads
    and make readers select the authoritative or newest copy instead.
    """
    url = f"{server.rstrip('/')}/rest/api/3/attachment/{attachment_id}"
    credentials = base64.b64encode(f"{user}:{token}".encode()).decode()
    headers = {
        "Authorization": f"Basic {credentials}",
        "Accept": "application/json",
    }
    last_error = None
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers=headers, method="DELETE")
            with urllib.request.urlopen(req, timeout=60, context=ssl_ctx):
                return None
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504):
                wait = max(int(e.headers.get("Retry-After", 1)), 4 ** attempt)
                print(f"  HTTP {e.code}, retrying in {wait}s...",
                      file=sys.stderr)
                time.sleep(wait)
                last_error = e
                continue
            raise
        except urllib.error.URLError as e:
            wait = 4 ** attempt
            print(f"  Network error: {e.reason}, retrying in {wait}s...",
                  file=sys.stderr)
            time.sleep(wait)
            last_error = e
    raise last_error


def get_transitions(server, user, token, issue_key):
    """GET available transitions for an issue."""
    path = f"/issue/{issue_key}/transitions"
    data = api_call_with_retry(server, path, user, token)
    return data.get("transitions", [])


def do_transition(server, user, token, issue_key, transition_id, fields=None):
    """POST a transition, optionally setting fields (e.g. resolution)."""
    body = {"transition": {"id": transition_id}}
    if fields:
        body["fields"] = fields
    path = f"/issue/{issue_key}/transitions"
    api_call_with_retry(server, path, user, token, body=body)


# ─── ADF Helpers ──────────────────────────────────────────────────────────────

def _adf_doc(content):
    """Wrap content nodes in an ADF document."""
    return {"type": "doc", "version": 1, "content": content}


def _adf_paragraph(text_nodes):
    """Create an ADF paragraph from text nodes."""
    return {"type": "paragraph", "content": text_nodes}


def _adf_text(text, marks=None):
    """Create an ADF text node, optionally with marks."""
    node = {"type": "text", "text": text}
    if marks:
        node["marks"] = marks
    return node


def _adf_heading(level, text_nodes):
    """Create an ADF heading node."""
    return {"type": "heading", "attrs": {"level": level},
            "content": text_nodes}


def _adf_code_block(text, language=""):
    """Create an ADF codeBlock node."""
    node = {"type": "codeBlock", "content": [_adf_text(text)]}
    if language:
        node["attrs"] = {"language": language}
    return node


def _adf_bullet_list(items):
    """Create an ADF bulletList from a list of content node lists."""
    return {
        "type": "bulletList",
        "content": [
            {"type": "listItem", "content": [_adf_paragraph(nodes)]}
            for nodes in items
        ],
    }


def _adf_ordered_list(items):
    """Create an ADF orderedList from a list of content node lists."""
    return {
        "type": "orderedList",
        "content": [
            {"type": "listItem", "content": [_adf_paragraph(nodes)]}
            for nodes in items
        ],
    }


def _adf_rule():
    """Create an ADF horizontal rule."""
    return {"type": "rule"}


def _adf_table(rows, has_header=True):
    """Create an ADF table from rows of cell text lists.

    Each row is a list of cell strings. If has_header, the first row
    uses tableHeader cells; remaining rows use tableCell.
    """
    adf_rows = []
    for row_idx, cells in enumerate(rows):
        is_header = has_header and row_idx == 0
        cell_type = "tableHeader" if is_header else "tableCell"
        adf_cells = []
        for cell_text in cells:
            adf_cells.append({
                "type": cell_type,
                "content": [_adf_paragraph(_parse_inline(cell_text.strip()))],
            })
        adf_rows.append({"type": "tableRow", "content": adf_cells})
    return {"type": "table", "content": adf_rows}


def _parse_inline(text):
    """Parse inline markdown formatting into ADF text nodes with marks.

    Handles: **bold**, *italic*, ~~strike~~, `code`, [text](url)
    """
    nodes = []
    pattern = re.compile(
        r'(\*\*(?P<bold>.+?)\*\*)'
        r'|(\*(?P<italic>.+?)\*)'
        r'|(~~(?P<strike>.+?)~~)'
        r'|(`(?P<code>[^`]+)`)'
        r'|(\[(?P<link_text>[^\]]*)\]\((?P<link_url>[^)]+)\))'
    )
    pos = 0
    for m in pattern.finditer(text):
        if m.start() > pos:
            nodes.append(_adf_text(text[pos:m.start()]))

        if m.group("bold") is not None:
            nodes.append(_adf_text(m.group("bold"), [{"type": "strong"}]))
        elif m.group("italic") is not None:
            nodes.append(_adf_text(m.group("italic"), [{"type": "em"}]))
        elif m.group("strike") is not None:
            nodes.append(_adf_text(m.group("strike"), [{"type": "strike"}]))
        elif m.group("code") is not None:
            nodes.append(_adf_text(m.group("code"), [{"type": "code"}]))
        elif m.group("link_text") is not None:
            nodes.append(_adf_text(
                m.group("link_text"),
                [{"type": "link",
                  "attrs": {"href": m.group("link_url")}}]
            ))
        pos = m.end()

    if pos < len(text):
        nodes.append(_adf_text(text[pos:]))

    return nodes if nodes else [_adf_text(text)]


def markdown_to_adf(markdown):
    """Convert markdown to Atlassian Document Format.

    Handles: headings, paragraphs, bullet/ordered lists, bold, italic,
    strikethrough, code spans, code blocks, blockquotes, tables,
    horizontal rules, links, and checkboxes (as text).
    """
    lines = markdown.split("\n")
    content = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # Code block
        if line.startswith("```"):
            lang = line[3:].strip()
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing ```
            content.append(_adf_code_block("\n".join(code_lines), lang))
            continue

        # Heading
        heading_match = re.match(r'^(#{1,6})\s+(.*)', line)
        if heading_match:
            level = len(heading_match.group(1))
            text = heading_match.group(2).strip()
            if text:
                content.append(_adf_heading(level, _parse_inline(text)))
            else:
                content.append(_adf_heading(level, [_adf_text("")]))
            i += 1
            continue

        # Horizontal rule
        if re.match(r'^---+\s*$', line):
            content.append(_adf_rule())
            i += 1
            continue

        # Bullet list
        if re.match(r'^[-*]\s', line) or re.match(r'^- \[[ x]\]\s', line):
            items = []
            while i < len(lines) and (re.match(r'^[-*]\s', lines[i]) or
                                       re.match(r'^- \[[ x]\]\s', lines[i])):
                item_text = re.sub(r'^[-*]\s+', '', lines[i])
                items.append(_parse_inline(item_text))
                i += 1
            content.append(_adf_bullet_list(items))
            continue

        # Ordered list
        if re.match(r'^\d+\.\s', line):
            items = []
            while i < len(lines) and re.match(r'^\d+\.\s', lines[i]):
                item_text = re.sub(r'^\d+\.\s+', '', lines[i])
                items.append(_parse_inline(item_text))
                i += 1
            content.append(_adf_ordered_list(items))
            continue

        # Blockquote
        if line.startswith("> ") or line == ">":
            quote_lines = []
            while i < len(lines) and (lines[i].startswith("> ") or
                                       lines[i] == ">"):
                quote_lines.append(re.sub(r'^>\s?', '', lines[i]))
                i += 1
            quote_md = "\n".join(quote_lines)
            inner = markdown_to_adf(quote_md)
            inner_content = inner.get("content", [])
            has_headings = any(n.get("type") == "heading"
                               for n in inner_content)
            if has_headings:
                # ADF blockquotes cannot contain headings, but panels
                # can. Quoted headings originate from Jira panels that
                # were converted to markdown blockquotes on fetch.
                content.append({
                    "type": "panel",
                    "attrs": {"panelType": "info"},
                    "content": inner_content,
                })
            else:
                content.append({
                    "type": "blockquote",
                    "content": inner_content,
                })
            continue

        # Table
        if re.match(r'^\|.+\|', line):
            table_rows = []
            while i < len(lines) and re.match(r'^\|.+\|', lines[i]):
                row_text = lines[i].strip()
                # Skip separator rows (| --- | --- |)
                if re.match(r'^\|[\s\-:|]+\|$', row_text):
                    i += 1
                    continue
                # Split cells, dropping empty first/last from leading/trailing |
                cells = row_text.split("|")
                cells = [c for c in cells[1:-1]]  # drop empty first/last
                table_rows.append(cells)
                i += 1
            if table_rows:
                content.append(_adf_table(table_rows, has_header=True))
            continue

        # Empty line — skip
        if not line.strip():
            i += 1
            continue

        # Paragraph — accumulate consecutive non-empty, non-special lines
        para_lines = []
        while i < len(lines) and lines[i].strip() and \
                not lines[i].startswith("#") and \
                not lines[i].startswith("```") and \
                not re.match(r'^[-*]\s', lines[i]) and \
                not re.match(r'^\d+\.\s', lines[i]) and \
                not re.match(r'^---+\s*$', lines[i]) and \
                not re.match(r'^\|.+\|', lines[i]):
            para_lines.append(lines[i])
            i += 1
        if para_lines:
            text = " ".join(para_lines)
            content.append(_adf_paragraph(_parse_inline(text)))
        else:
            # Safety net: no branch matched and i was not advanced.
            # Treat the line as a standalone paragraph to prevent
            # infinite loops on any unrecognized line format.
            content.append(_adf_paragraph(_parse_inline(line)))
            i += 1

    return _adf_doc(content) if content else \
        _adf_doc([_adf_paragraph([_adf_text("")])])


def text_to_adf_codeblock(text):
    """Wrap raw text in a single ADF codeBlock — for archival comments."""
    return _adf_doc([_adf_code_block(text)])


def text_to_adf_paragraph(text):
    """Wrap text in a simple ADF paragraph — for short status comments."""
    return _adf_doc([_adf_paragraph([_adf_text(text)])])


def archival_comment_adf(header, markdown_body):
    """Build ADF for an archival comment: header paragraph + codeBlock body."""
    return _adf_doc([
        _adf_paragraph(_parse_inline(header)),
        _adf_code_block(markdown_body),
    ])


# ─── ADF → Markdown ──────────────────────────────────────────────────────────

def adf_to_markdown(node, list_depth=0):
    """Convert Atlassian Document Format (ADF) JSON to markdown."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node

    if isinstance(node, list):
        return "".join(adf_to_markdown(item, list_depth) for item in node)

    if not isinstance(node, dict):
        return ""

    node_type = node.get("type", "")
    content = node.get("content", [])
    attrs = node.get("attrs", {})

    if node_type == "doc":
        return adf_to_markdown(content, list_depth)

    if node_type == "text":
        text = node.get("text", "")
        for mark in node.get("marks", []):
            mark_type = mark.get("type", "")
            if mark_type == "strong":
                text = f"**{text}**"
            elif mark_type == "em":
                text = f"*{text}*"
            elif mark_type == "code":
                text = f"`{text}`"
            elif mark_type == "strike":
                text = f"~~{text}~~"
            elif mark_type == "link":
                href = mark.get("attrs", {}).get("href", "")
                text = f"[{text}]({href})"
        return text

    if node_type == "paragraph":
        inner = adf_to_markdown(content, list_depth)
        return f"{inner}\n\n"

    if node_type == "heading":
        level = attrs.get("level", 1)
        inner = adf_to_markdown(content, list_depth)
        return f"{'#' * level} {inner}\n\n"

    if node_type == "bulletList":
        items = adf_to_markdown(content, list_depth)
        return f"{items}\n" if list_depth == 0 else items

    if node_type == "orderedList":
        result = []
        for idx, item in enumerate(content, 1):
            item_text = adf_to_markdown(
                item.get("content", []), list_depth + 1
            ).strip()
            indent = "  " * list_depth
            result.append(f"{indent}{idx}. {item_text}\n")
        return "".join(result) + ("\n" if list_depth == 0 else "")

    if node_type == "listItem":
        item_parts = []
        for child in content:
            child_type = child.get("type", "")
            if child_type in ("bulletList", "orderedList"):
                item_parts.append(adf_to_markdown(child, list_depth + 1))
            else:
                item_parts.append(
                    adf_to_markdown(child, list_depth).strip()
                )
        indent = "  " * list_depth
        first = item_parts[0] if item_parts else ""
        rest = "".join(item_parts[1:])
        return f"{indent}- {first}\n{rest}"

    if node_type == "codeBlock":
        lang = attrs.get("language", "")
        inner = adf_to_markdown(content, list_depth)
        return f"```{lang}\n{inner}\n```\n\n"

    if node_type == "blockquote":
        inner = adf_to_markdown(content, list_depth)
        lines = inner.strip().split("\n")
        quoted = "\n".join(f"> {line}" for line in lines)
        return f"{quoted}\n\n"

    if node_type == "rule":
        return "---\n\n"

    if node_type == "table":
        rows = []
        for row_node in content:
            if row_node.get("type") == "tableRow":
                cells = []
                for cell in row_node.get("content", []):
                    cell_text = adf_to_markdown(
                        cell.get("content", []), list_depth
                    ).strip()
                    cell_text = cell_text.replace("\n", " ")
                    cells.append(cell_text)
                rows.append(cells)
        if not rows:
            return ""
        col_count = max(len(r) for r in rows)
        lines = []
        for i, row in enumerate(rows):
            row += [""] * (col_count - len(row))
            lines.append("| " + " | ".join(row) + " |")
            if i == 0:
                lines.append("| " + " | ".join(["---"] * col_count) + " |")
        return "\n".join(lines) + "\n\n"

    if node_type in ("mediaSingle", "media"):
        return ""

    if node_type == "hardBreak":
        return "\n"

    if node_type == "inlineCard":
        url = attrs.get("url", "")
        return f"[{url}]({url})" if url else ""

    if node_type == "emoji":
        return attrs.get("text", attrs.get("shortName", ""))

    if node_type == "panel":
        inner = adf_to_markdown(content, list_depth)
        lines = inner.strip().split("\n")
        quoted = "\n".join(f"> {line}" for line in lines)
        return f"{quoted}\n\n"

    if node_type == "expand":
        title = attrs.get("title", "")
        inner = adf_to_markdown(content, list_depth)
        header = f"**{title}**\n\n" if title else ""
        return f"{header}{inner}"

    # Fallback: recurse into content
    return adf_to_markdown(content, list_depth)


# ─── Content Processing ──────────────────────────────────────────────────────

def strip_metadata(markdown):
    """Remove artifact metadata and revision notes from RFE markdown.

    Strips content that should not be pushed to Jira:
    - YAML frontmatter (--- delimited block at start of file)
    - Title headings (# RFE-NNN: / # RHAIRFE-NNN: / # STRAT-NNN: / # RHAISTRAT-NNN:)
      — title is in frontmatter and Jira's summary field
    - Legacy inline metadata lines (now in frontmatter):
      **Jira Key**, **Size**, **Split from**, **Priority**, **Source RFE**
    - Legacy revision notes (now in review files):
      ### Revision Notes sections, > *Review note: ...* blockquotes
    - ALL HTML comments (<!-- ... -->) — these are invisible in Jira's
      rendered view and should never be pushed
    """
    # Strip YAML frontmatter if present
    frontmatter_match = re.match(r'^---\s*\n.*?\n---\s*\n', markdown,
                                 re.DOTALL)
    if frontmatter_match:
        markdown = markdown[frontmatter_match.end():]

    # Strip all HTML comments (invisible in Jira rendered view)
    markdown = re.sub(r'<!--.*?-->', '', markdown, flags=re.DOTALL)

    lines = markdown.split("\n")
    result = []
    in_revision_notes = False

    for line in lines:
        # Skip title heading — duplicates Summary
        if re.match(r'^#\s+(RFE-\d+|RHAIRFE-\d+|STRAT-\d+|RHAISTRAT-\d+):',
                    line):
            continue

        # Skip metadata lines (legacy inline format, now in frontmatter)
        if re.match(r'^\*\*(Jira Key|Size|Split from|Priority|'
                    r'Source RFE)\*\*:', line):
            continue

        # Skip review note blockquotes
        if re.match(r'^>\s*\*Review note:', line):
            continue

        # Track revision notes section
        if re.match(r'^###\s+Revision Notes', line):
            in_revision_notes = True
            continue
        if in_revision_notes:
            if re.match(r'^##\s', line):
                in_revision_notes = False
            else:
                continue

        result.append(line)

    # Clean up multiple consecutive blank lines
    cleaned = "\n".join(result)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()


def normalize_for_compare(text):
    """Normalize text to ignore ADF-to-markdown conversion artifacts.

    Handles: curly quotes, non-breaking spaces, carriage returns,
    dash/arrow variants, trailing whitespace, emoji, table alignment,
    and other Unicode normalization differences.
    """
    # Unicode normalize (NFC)
    text = unicodedata.normalize("NFC", text)
    # Carriage returns
    text = text.replace("\r", "")
    # Curly quotes -> straight
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    # Dashes: em dash -> —, en dash -> -  (normalize to ASCII)
    text = text.replace("\u2014", "---").replace("\u2013", "--")
    # Arrows: → -> ->
    text = text.replace("\u2192", "->")
    # Non-breaking space -> regular space
    text = text.replace("\xa0", " ")
    # Collapse multiple spaces to one (table alignment differences)
    text = re.sub(r"  +", " ", text)
    # Strip emoji (Unicode emoji blocks)
    text = re.sub(
        r"[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF"
        r"\U0001F680-\U0001F6FF\U0001F900-\U0001F9FF"
        r"\U00002702-\U000027B0\U0000FE00-\U0000FE0F]", "", text)
    # Normalize table separator rows (varying dash counts)
    text = re.sub(r"-{2,}", "--", text)
    # Strip auto-linked URLs: [url](url) -> url
    text = re.sub(r"\[([^\]]+)\]\(\1/?\.?\)", r"\1", text)
    # Strip zero-width characters
    text = re.sub(r"[\u200b\u200c\u200d\u2060\ufeff]", "", text)
    # Strip trailing whitespace per line
    text = re.sub(r"[ \t]+$", "", text, flags=re.MULTILINE)
    # Collapse multiple blank lines to one
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# --- RFE reference utilities (shared by push/pull/create) ---

BUSINESS_NEED_HEADING = "## Business Need (from RFE)"
RFE_REFERENCE_MARKER = "The full business need is maintained in the source RFE:"
_STRATEGY_HEADING_FOR_REF = "## Strategy (AI Generated by Agentic SDLC Pipeline)"


def build_rfe_reference(rfe_key, server):
    """Build a compact Business Need section with a link to the source RFE."""
    url = f"{server.rstrip('/')}/browse/{rfe_key}"
    return (
        f"{BUSINESS_NEED_HEADING}\n\n"
        f"> {RFE_REFERENCE_MARKER} "
        f"[{rfe_key}]({url})"
    )


def reconstruct_business_need(description_md, rfe_md):
    """Replace a Business Need reference with full RFE content.

    If description_md contains the RFE_REFERENCE_MARKER and a Strategy
    heading, replaces everything before the Strategy heading with the
    full RFE content under a Business Need heading.

    Returns description_md unchanged if no reference marker is found.
    """
    if RFE_REFERENCE_MARKER not in description_md or not rfe_md:
        return description_md
    if _STRATEGY_HEADING_FOR_REF in description_md:
        idx = description_md.index(_STRATEGY_HEADING_FOR_REF)
        return (
            f"{BUSINESS_NEED_HEADING}\n\n"
            f"{rfe_md}\n\n"
            f"{description_md[idx:]}"
        )
    return description_md


def reconstruct_business_need_file(strategy_path, rfe_path):
    """Replace RFE reference in a strategy file with full RFE content.

    Handles YAML frontmatter — only the body is modified.
    Returns True if reconstruction was performed, False otherwise.
    """
    with open(strategy_path, "r", encoding="utf-8") as f:
        content = f.read()

    if RFE_REFERENCE_MARKER not in content:
        return False

    if not os.path.exists(rfe_path):
        print(f"  WARNING: RFE original not found at {rfe_path}; "
              "cannot reconstruct Business Need.", file=sys.stderr)
        return False

    with open(rfe_path, "r", encoding="utf-8") as f:
        rfe_md = f.read().strip()

    if not rfe_md:
        return False

    fm_match = re.match(r'^(---\s*\n.*?\n---\s*\n?)', content, re.DOTALL)
    if fm_match:
        frontmatter = fm_match.group(1)
        if not frontmatter.endswith("\n"):
            frontmatter += "\n"
        body = content[fm_match.end():]
    else:
        frontmatter = ""
        body = content

    new_body = reconstruct_business_need(body, rfe_md)
    if new_body == body:
        return False

    with open(strategy_path, "w", encoding="utf-8") as f:
        f.write(frontmatter + new_body)
    return True
