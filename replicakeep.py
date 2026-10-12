#!/usr/bin/env python
"""Plan, then repair, an idle replica that holds the geodatabase compress floor.

Compress on a traditionally versioned geodatabase exits 0 whether or not it got
anywhere. A replica keeps hidden SYNC_ system versions (anchors), and compress
cannot fold the states those versions still point at. In the logs this tool was
built from, a collaboration replica acknowledged a new generation only after a
sync that carried an in-scope edit. A layer nobody edited kept acknowledging
the same generation, and its anchors stayed where they were. That is an
observation from one estate, not a documented rule, and this tool words it so.

It reads three exports (the versions, the replica items and the replica log),
finds the server-type replicas that hold anchors, sync, and have not moved
their acknowledged generation for a set number of days, and plans one edit per
dataset: rewrite one attribute of the lowest OBJECTID that satisfies the
replica's own filter, with the value it already has. The edit is a real edit
that changes no data. If editor tracking is on, it restamps the editor and
edit date of that one row.

Without --apply nothing is written and no network is used. With --apply the
tool edits through the ArcGIS REST API of the feature service you name in
--targets: it reads every layer and picks every row first, and writes only if
all of those reads succeed. It never deletes a version, never unregisters a
replica and never touches a database. A replica that holds anchors but does
not sync cannot be repaired by an edit. The tool says so and leaves it to
compressfloor's --unregister-plan.

    python replicakeep.py --self-test
    python replicakeep.py --versions v.csv --replicas r.csv --replica-log l.csv
    python replicakeep.py ... --min-idle-days 14 --silent-days 3 --only 101,102
    python replicakeep.py ... --targets targets.csv --report plan.txt --apply

The token for --apply comes from the REPLICAKEEP_TOKEN environment variable
only. It is sent in the X-Esri-Authorization header, never in a URL, and never
printed.

Exit codes: 0 no finding of the --exit-on level, 1 at least one, 2 the tool
could not do its job (bad input, a refused URL, a failed read or write).
By default (--exit-on idle) a finding is an idle replica that the run did not
repair, and any other replica that holds anchors and that an edit cannot move
or the tool cannot judge (not syncing, no known generation, nothing to edit,
no recognised TargetType, a log too short to show how long a generation stood
still, or an edit that could not be checked).
--exit-on any also counts an anchor with no replica item. --exit-on never keeps
exit 2 only.
"""

import argparse
import contextlib
import csv
import datetime
import io
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

# A weekly run has no use for a replica that moved within the last week.
DEFAULT_MIN_IDLE_DAYS = 6
# A replica with no logged event for longer than this is not syncing, and an
# edit cannot help it.
DEFAULT_SILENT_DAYS = 3
# The most edits one --apply run will make. A plan larger than this is refused.
DEFAULT_MAX_EDITS = 10
# GDB_REPLICALOG.errorcode of a good event. Esri says only that "a successful
# event gets a success code". 0 is what the logs this was built from showed.
SUCCESS_CODE = 0
# GDB_REPLICALOG.event: Esri defines 1 as an import and 2 as an export.
DEFAULT_EVENT = 1
REQUEST_TIMEOUT = 30
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
TOKEN_ENV = "REPLICAKEEP_TOKEN"

# Esri's esriReplicaTargetType enum: Undefined (0), Client (1), Server (2).
SERVER_TYPE = "esriReplicaTargetTypeServer"
CLIENT_TYPE = "esriReplicaTargetTypeClient"
DATASET_TYPES = ("esriDTFeatureClass", "esriDTTable")
# Field types that are never rewritten. A rewrite of these is not a no-op.
SKIP_FIELD_TYPES = ("esriFieldTypeOID", "esriFieldTypeGeometry",
                    "esriFieldTypeGlobalID", "esriFieldTypeGUID",
                    "esriFieldTypeBlob", "esriFieldTypeRaster",
                    "esriFieldTypeXML")
LOOPBACK = ("127.0.0.1", "::1", "localhost")
LAYER_RE = re.compile(r"/FeatureServer/[0-9]+/?$", re.I)
# An optional owner prefix, as arcpy ListVersions returns it (OWNER.SYNC_...).
SYNC_RE = re.compile(r"^(?:[^.\s]+\.)*SYNC_(SEND|RECEIVE_REC|RECEIVE)_"
                     r"([0-9]+)_([0-9]+)$", re.I)
EXIT_ON = ("idle", "any", "never")

IDLE = "IDLE"
COVERED = "COVERED"
ACTIVE = "ACTIVE"
NOT_SYNCING = "NOT_SYNCING"
NO_ACK = "NO_ACK"
NO_DATASET = "NO_DATASET"
CLIENT = "CLIENT"
NO_TYPE = "NO_TYPE"
SHORT_LOG = "SHORT_LOG"
NO_ANCHOR = "NO_ANCHOR"
EXCLUDED = "EXCLUDED"
# Anchors held, and no edit moves them or the tool could not judge. A clean
# exit here would be the false green this tool exists to end.
UNJUDGED = (NOT_SYNCING, NO_ACK, NO_DATASET, NO_TYPE, SHORT_LOG)

REQUIRED = {
    "versions": ("name",),
    "replicas": ("objectid", "item_type", "definition"),
    "replica_log": ("replicaid", "logdate", "event", "errorcode",
                    "targetgen"),
    "targets": ("dataset", "layer_url"),
}
COUNTED = ("versions", "replicas", "replica_log")
ROW_COUNT = "row_count"
EXPORTED_AT = "exported_at"
KNOWN_COLUMNS = set(c for cols in REQUIRED.values() for c in cols) | set(
    [ROW_COUNT, EXPORTED_AT])


class InputError(Exception):
    """An input the tool cannot reason over. Exit 2, never a finding."""


class ApplyError(Exception):
    """A layer, row or request the tool will not read or write. Exit 2."""


# --------------------------------------------------------------- decision core

TIME_RE = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})"
                     r"(?:[ T]([0-9]{2}:[0-9]{2}:[0-9]{2})(?:\.[0-9]+)?"
                     r"(?:Z|[+-][0-9]{2}(?::?[0-9]{2})?)?)?\Z")


def parse_time(text):
    """A timestamp from an export, or None for a blank cell.

    Accepts YYYY-MM-DD, and YYYY-MM-DD HH:MM:SS with a T separator, a fraction
    and a zone. The zone is ignored: every timestamp comes from one database
    clock. Any other suffix raises ValueError, because "03:00:00 PM" read as
    03:00 moves a clock 12 hours.
    """
    t = str(text).strip()
    if not t:
        return None
    m = TIME_RE.match(t)
    if not m:
        raise ValueError("not a YYYY-MM-DD HH:MM:SS timestamp")
    if m.group(2) is None:
        return datetime.datetime.strptime(m.group(1), "%Y-%m-%d")
    return datetime.datetime.strptime("%s %s" % m.groups(),
                                      "%Y-%m-%d %H:%M:%S")


def to_int(value):
    """An integer cell. 17, "17" and "17.0" are 17; anything else raises.

    Only ValueError escapes. int(float("inf")) raises OverflowError, which no
    caller catches, and an uncaught error would exit 1: the code for a finding.
    """
    text = str(value).strip()
    try:
        return int(text)
    except ValueError:
        number = float(text)
        if not math.isfinite(number) or number != int(number):
            raise ValueError("not a whole number")
        return int(number)


def clean(value):
    """Text with every unprintable character escaped.

    Names reach stdout, the --report file and error messages. A newline in a
    replica name could forge a clean VERDICT line, and an ANSI escape could
    hide the real one in a terminal.
    """
    text = str(value or "").strip()
    return text if text.isprintable() else (
        text.encode("unicode_escape").decode("ascii"))


def cell(row, column, kind, index, convert):
    """One converted cell, or an InputError naming the export and the row.

    The message never holds the cell's value: a wrong file passed as an export
    may hold a secret.
    """
    try:
        return convert(row.get(column) if row.get(column) is not None else "")
    except ValueError:
        raise InputError("%s export row %d, column %s: the value is not %s"
                         % (kind, index + 1, column,
                            "a whole number" if convert is to_int
                            else "a YYYY-MM-DD HH:MM:SS time"))


def parse_definition(xml):
    """One replica item as a dict, or None for an item this tool cannot use.

    None covers a blank definition, a root other than GPReplica (a Sync
    Replica item is a GPSyncReplica) and an ID that is missing, not a whole
    number or -1. The element names (ID, Name, TargetType, GPReplicaDataset,
    DatasetName, DatasetType, DefQuery) were observed in the definitions this
    tool was built from. Esri documents only /GPReplica/ID and /GPReplica/Name.
    """
    if not xml.strip():
        return None
    if re.search(r"<!(DOCTYPE|ENTITY)", xml, re.I):
        # No real definition has a DTD. An entity bomb must not reach expat.
        raise InputError("a replica definition holds a DTD or an entity. "
                         "Real definitions hold neither, so the export is "
                         "refused.")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise InputError("a replica definition is not XML.")
    if root.tag != "GPReplica":
        return None
    text = (root.findtext("ID") or "").strip()
    rid = int(text) if re.match(r"^[0-9]+$", text) else -1
    if rid < 0:
        return None
    datasets = []
    for el in root.iter("GPReplicaDataset"):
        name = clean(el.findtext("DatasetName"))
        if el.findtext("DatasetType") in DATASET_TYPES and name:
            datasets.append((name, (el.findtext("DefQuery") or "").strip()))
    return {"id": rid, "name": clean(root.findtext("Name")) or "(unnamed)",
            "target_type": (root.findtext("TargetType") or "").strip(),
            "datasets": datasets}


def build(versions, replicas, replica_log, as_of=None):
    """The model: anchors by replica id, replicas, log rows by item objectid.

    The database clock is the exported_at of the versions export, so the same
    exports give the same plan on any day. as_of replaces it.
    """
    if not versions:
        raise InputError("the versions export is empty. A failed export "
                         "leaves an empty file, and read as no versions it "
                         "would call every replica clean.")
    if not replicas:
        raise InputError("the replicas export is empty. A failed export "
                         "leaves an empty file, and read as no replicas it "
                         "would call every anchor detached.")
    if as_of is None:
        if len(set(clean(r.get(EXPORTED_AT)) for r in versions)) != 1:
            raise InputError("the versions export does not carry one "
                             "exported_at time on every row.")
        as_of = cell(versions[0], EXPORTED_AT, "versions", 0, parse_time)
        if as_of is None:
            raise InputError("the versions export has a blank exported_at.")
    anchors, odd = {}, 0
    for r in versions:
        name = clean(r.get("name"))
        m = SYNC_RE.match(name)
        if m:
            anchors.setdefault(int(m.group(2)), []).append(name)
        elif "SYNC_" in name.upper():
            # Looks like an anchor, matches no pattern. Silence here would
            # call every replica clean.
            odd += 1
    reps, unread, objectids = [], 0, set()
    for i, r in enumerate(replicas):
        objectid = cell(r, "objectid", "replicas", i, to_int)
        parsed = parse_definition(str(r.get("definition") or ""))
        if objectid in objectids:
            raise InputError("the replicas export has one objectid twice.")
        objectids.add(objectid)
        if parsed is None:
            unread += 1
        else:
            parsed["objectid"] = objectid
            reps.append(parsed)
    if len(set(r["id"] for r in reps)) != len(reps):
        raise InputError("two replica items carry one replica ID. The tool "
                         "cannot tell which one owns the anchors.")
    if anchors and not reps:
        raise InputError("anchor versions exist, but no item in the replicas "
                         "export is a usable GPReplica, so every anchor would "
                         "read as detached. Check the item type and the XML.")
    log = {}
    for i, r in enumerate(replica_log):
        when = cell(r, "logdate", "replica_log", i, parse_time)
        if when is None:
            raise InputError("replica_log export row %d has a blank logdate."
                             % (i + 1))
        log.setdefault(cell(r, "replicaid", "replica_log", i, to_int),
                       []).append(
            (when, cell(r, "event", "replica_log", i, to_int),
             cell(r, "errorcode", "replica_log", i, to_int),
             cell(r, "targetgen", "replica_log", i, to_int)))
    for rows in log.values():
        rows.sort()
    judged = set(r["objectid"] for r in reps
                 if r["id"] in anchors and r["target_type"] == SERVER_TYPE)
    if judged and not judged & set(log):
        raise InputError("the replica_log export holds no row for any replica "
                         "item that holds anchors. An empty or truncated log, "
                         "or one keyed by the replica ID instead of the item "
                         "objectid, would call every replica not syncing.")
    newest = max([row[0] for rows in log.values() for row in rows] or [as_of])
    if as_of < newest:
        raise InputError("the as-of time is earlier than the newest replica "
                         "log row, so an age would be negative.")
    return {"as_of": as_of, "anchors": anchors, "replicas": reps,
            "unread": unread, "odd": odd, "log": log}


def analyse(model, min_idle_days=DEFAULT_MIN_IDLE_DAYS,
            silent_days=DEFAULT_SILENT_DAYS, event=DEFAULT_EVENT, only=None):
    """Verdict per replica, the edit plan and the anchors with no replica."""
    as_of = model["as_of"]
    unknown = sorted((only or set()) - set(r["id"] for r in model["replicas"]))
    if unknown:
        raise InputError("--only names replica id(s) %s, which no replica "
                         "item carries." % ", ".join(str(i) for i in unknown))
    verdicts, edits, chosen = [], [], {}
    for rep in sorted(model["replicas"], key=lambda r: r["id"]):
        v = {"id": rep["id"], "name": rep["name"], "status": None,
             "detail": "", "edit": None}
        verdicts.append(v)
        anchors = model["anchors"].get(rep["id"], [])
        rows = model["log"].get(rep["objectid"], [])
        ok = [r for r in rows if r[2] == SUCCESS_CODE]
        good = [r for r in ok if r[1] == event]
        if only is not None and rep["id"] not in only:
            v["status"] = EXCLUDED
        elif not anchors:
            v["status"] = NO_ANCHOR
        elif rep["target_type"] == CLIENT_TYPE:
            v["status"] = CLIENT
            v["detail"] = ("a client-type replica, so it is never edited. It "
                           "holds anchors, and it is listed but never a "
                           "finding")
        elif rep["target_type"] != SERVER_TYPE:
            # Undefined, absent or unrecognised. Calling it a client would be
            # a clean exit for a replica that may be idle.
            v["status"] = NO_TYPE
            v["detail"] = ("holds %d anchor version(s), and its TargetType is "
                           "missing, Undefined or not recognised, so the tool "
                           "cannot say whether an edit moves it"
                           % len(anchors))
        elif not ok or as_of - ok[-1][0] > datetime.timedelta(
                days=silent_days):
            v["status"] = NOT_SYNCING
            v["detail"] = ("holds %d anchor version(s) and logged no "
                           "successful event in the last %d day(s). An edit "
                           "cannot help a replica that does not sync. If it "
                           "syncs less often, raise --silent-days. If it is "
                           "abandoned, compressfloor --unregister-plan "
                           "orders its removal."
                           % (len(anchors), silent_days))
        elif not good:
            v["status"] = NO_ACK
            v["detail"] = ("holds %d anchor version(s) and syncs, but the "
                           "log holds no successful row of event %d, so the "
                           "acknowledged generation is unknown"
                           % (len(anchors), event))
        else:
            moved = None
            for before, after in zip(good, good[1:]):
                if after[3] != before[3]:
                    moved = after[0]
            # No change of generation in the rows kept: the first row is only
            # a lower bound on how long the generation has stood still.
            bound = moved is None
            idle = (as_of - (good[0][0] if bound else moved)).days
            gen = good[-1][3]
            span = ("at least %d" if bound else "%d") % idle
            if idle < min_idle_days and bound:
                v["status"] = SHORT_LOG
                v["detail"] = ("generation %d showed no change in the %d "
                               "day(s) the log covers, under the %d-day "
                               "limit, so how long it has stood still is "
                               "unknown. Export the whole replica log."
                               % (gen, idle, min_idle_days))
            elif idle < min_idle_days:
                v["status"] = ACTIVE
                v["detail"] = ("generation %d moved %d day(s) ago, under "
                               "the %d-day limit" % (gen, idle,
                                                     min_idle_days))
            elif not rep["datasets"]:
                v["status"] = NO_DATASET
                v["detail"] = ("generation %d unchanged for %s day(s), but "
                               "the definition lists no feature class or "
                               "table to edit" % (gen, span))
            else:
                v["detail"] = ("generation %d unchanged for %s day(s); "
                               "holds %d anchor version(s)"
                               % (gen, span, len(anchors)))
                keys = [(d[0].upper(), d[1]) for d in rep["datasets"]]
                hit = next((k for k in keys if k in chosen), None)
                if hit is None:
                    edit = {"replica": rep["id"], "name": rep["name"],
                            "dataset": rep["datasets"][0][0],
                            "query": rep["datasets"][0][1],
                            "covers": [rep["id"]], "target": None}
                    chosen[keys[0]] = edit
                    edits.append(edit)
                    v["status"], v["edit"] = IDLE, edit
                else:
                    chosen[hit]["covers"].append(rep["id"])
                    v["status"], v["edit"] = COVERED, chosen[hit]
    known = set(r["id"] for r in model["replicas"])
    detached = sorted(i for i in model["anchors"] if i not in known)
    return {"verdicts": verdicts, "edits": edits, "detached": detached,
            "as_of": as_of, "unread": model["unread"], "odd": model["odd"]}


def resolve_target(dataset, targets):
    """The layer URL for a dataset name, or None unless exactly one matches.

    A target names the dataset in full or by its trailing parts, so
    OWNER.TABLE and TABLE both match DB.OWNER.TABLE.
    """
    d = dataset.upper()
    hits = [t for t in targets if d == clean(t["dataset"]).upper()
            or d.endswith("." + clean(t["dataset"]).upper())]
    return clean(hits[0]["layer_url"]) if len(hits) == 1 else None


def gate(report, exit_on, repaired=()):
    """The exit code. Exit 2 is decided elsewhere and wins over this.

    Besides replicas, a finding is a version name that looks like an anchor
    and matches no pattern, and an anchor whose replica item is missing while
    another item could not be read.
    """
    found = [v for v in report["verdicts"]
             if v["status"] in (IDLE, COVERED) and v["id"] not in repaired
             or v["status"] in UNJUDGED]
    if exit_on == "never":
        return 0
    # An anchor with no readable item may belong to an item that was not read.
    # The tool cannot judge it, so it is a finding as an unjudged replica is.
    unattributed = report["detached"] and report["unread"]
    if found or unattributed or report["odd"] or (
            exit_on == "any" and report["detached"]):
        return 1
    return 0


def describe(report, min_idle_days, applied=None, apply=False):
    """The report as a list of lines."""
    shown = [v for v in report["verdicts"]
             if v["status"] not in (NO_ANCHOR, EXCLUDED)]
    lines = ["replicakeep: idle replicas that may hold the compress floor",
             "as of %s, the database clock of the versions export"
             % report["as_of"].strftime("%Y-%m-%d %H:%M:%S"),
             "This is an observation. An edit is the repair that was seen to "
             "work; the proof is a later export showing the generation moved.",
             ""]
    for v in shown:
        lines.append("%-11s replica %d '%s': %s"
                     % (v["status"], v["id"], v["name"], v["detail"]))
    hidden = len(report["verdicts"]) - len(shown)
    lines.append("%d replica item(s) hold no anchor or were left out by "
                 "--only; %d item(s) were not a usable GPReplica."
                 % (hidden, report["unread"]))
    if report["detached"]:
        lines.append("ANCHOR WITHOUT A REPLICA ITEM: replica id(s) %s. %s Run "
                     "compressfloor, which names them."
                     % (", ".join(str(i) for i in report["detached"]),
                        "%d item(s) were not a usable GPReplica and one may "
                        "own these, so the tool cannot judge them."
                        % report["unread"] if report["unread"]
                        else "No edit helps these."))
    if report["odd"]:
        lines.append("%d version name(s) hold SYNC_ but match no anchor "
                     "pattern (SYNC_SEND_, SYNC_RECEIVE_ or SYNC_RECEIVE_REC_ "
                     "then replica id and generation), so the tool cannot "
                     "judge them." % report["odd"])
    lines += ["", "PLAN"]
    for n, e in enumerate(report["edits"], 1):
        lines.append("  edit %d of %d for replica %d (covers %s): dataset %s, "
                     "filter %s" % (n, len(report["edits"]), e["replica"],
                                    ", ".join(str(i) for i in e["covers"]),
                                    e["dataset"],
                                    clean(e["query"]) or "none"))
        lines.append("    rewrite one attribute of the lowest OBJECTID that "
                     "satisfies the filter with its own value.")
        lines.append("    target: %s" % (
            "not shown, the URL holds a query string or login"
            if e["target"] and holds_secret(e["target"])
            else e["target"] or "none"))
        if applied is not None:
            lines.append("    %s" % applied[e["replica"]])
    if not report["edits"]:
        lines.append("  no edit planned")
    lines.append("")
    idle = len([v for v in report["verdicts"] if v["status"] in (IDLE,
                                                                 COVERED)])
    unjudged = len([v for v in report["verdicts"] if v["status"] in UNJUDGED])
    marks = [a.split(" ")[0] for a in (applied or {}).values()]
    if not apply:
        ending = "Nothing was written."
    elif not marks:
        ending = "No edit was planned, so nothing was written."
    elif "FAILED" in marks:
        ending = "An edit FAILED. Read the lines above."
    elif "UNVERIFIED" in marks:
        ending = ("Every edit was sent, but at least one could not be "
                  "checked. Read the lines above.")
    else:
        ending = "Every edit was made and checked."
    extra = ""
    if report["detached"] and report["unread"]:
        extra += "; %d anchor replica id(s) have no readable item" % len(
            report["detached"])
    if report["odd"]:
        extra += "; %d SYNC_ version name(s) match no anchor pattern" % (
            report["odd"])
    lines.append("VERDICT: %d idle replica(s) at %d day(s) or more, %d "
                 "edit(s) planned; %d more hold anchors that an edit cannot "
                 "move or that could not be judged%s. %s" % (
                     idle, min_idle_days, len(report["edits"]), unjudged,
                     extra, ending))
    return lines


# ------------------------------------------------------------------ the writes

def holds_secret(url):
    """True if a URL could carry a credential: a query, a fragment or a login."""
    p = urllib.parse.urlsplit(url)
    return bool(p.query or p.fragment or p.username or p.password)


def check_url(url):
    """Raise unless the URL is a feature service layer this tool may call.

    The token must not cross plain http except to the machine itself. A URL
    with a query string or a login in it may hold a credential, and is refused
    without echoing it.
    """
    p = urllib.parse.urlsplit(url)
    if holds_secret(url):
        raise ApplyError("a target URL must hold no query string, fragment "
                         "or login. The token goes in %s." % TOKEN_ENV)
    if p.scheme == "http" and p.hostname in LOOPBACK:
        return
    if p.scheme != "https" or not p.hostname:
        raise ApplyError("a target URL must be https, or http to the local "
                         "machine.")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse a redirect: urllib would resend the token header to its target."""

    def redirect_request(self, *args, **kwargs):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def call(url, params, token, post=True):
    """One REST call. Returns the parsed JSON, or raises ApplyError."""
    check_url(url)
    body = urllib.parse.urlencode(params)
    if post:
        req = urllib.request.Request(url, data=body.encode("ascii"))
    else:
        req = urllib.request.Request(url + "?" + body)
    if token:
        req.add_header("X-Esri-Authorization", "Bearer " + token)
    try:
        with OPENER.open(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
    except (urllib.error.URLError, OSError) as exc:
        raise ApplyError("the request failed: %s" % clean(exc)[:200])
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ApplyError("the response is larger than %d bytes."
                         % MAX_RESPONSE_BYTES)
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise ApplyError("the response is not JSON.")
    if not isinstance(data, dict):
        raise ApplyError("the response is not a JSON object.")
    if "error" in data:
        err = data["error"] if isinstance(data["error"], dict) else {}
        raise ApplyError("the service returned an error: code %s, %s"
                         % (clean(err.get("code")),
                            clean(err.get("message"))[:200]))
    return data


def read_row(url, token, pick, objectid):
    """The attributes of one row, without geometry."""
    names = [pick["oid"], pick["field"]] + (
        [pick["stamp"]] if pick["stamp"] else [])
    data = call(url + "/query", {"objectIds": str(objectid),
                                 "outFields": ",".join(names),
                                 "returnGeometry": "false", "f": "json"},
                token)
    feats = data.get("features")
    if not isinstance(feats, list) or len(feats) != 1:
        raise ApplyError("OBJECTID %d did not come back as one row."
                         % objectid)
    attrs = feats[0].get("attributes") if isinstance(feats[0], dict) else None
    if not isinstance(attrs, dict) or pick["field"] not in attrs:
        raise ApplyError("the row has no value for the field to rewrite.")
    return attrs


def preflight(edit, token):
    """Read the layer and the row an edit would rewrite. Writes nothing."""
    url = edit["target"]
    if not LAYER_RE.search(urllib.parse.urlsplit(url).path):
        raise ApplyError("a target must be a feature service layer URL, "
                         "ending in /FeatureServer/<number>.")
    info = call(url, {"f": "json"}, token, post=False)
    caps = [c.strip().lower() for c in str(info.get("capabilities")).split(",")]
    if "update" not in caps:
        raise ApplyError("the layer does not list Update in its "
                         "capabilities.")
    if info.get("isDataBranchVersioned") is True:
        raise ApplyError("the layer is branch versioned. This tool edits "
                         "the version the service is published against, "
                         "which is not where a branch edit would go.")
    fields = [f for f in info.get("fields") or [] if isinstance(f, dict)]
    oids = [f.get("name") for f in fields
            if f.get("type") == "esriFieldTypeOID"]
    track = info.get("editFieldsInfo")
    track = track if isinstance(track, dict) else {}
    tracked = set(str(v).lower() for v in track.values()
                  if isinstance(v, str))
    field = next((f["name"] for f in fields
                  if f.get("editable") is True
                  and f.get("type") not in SKIP_FIELD_TYPES
                  and str(f.get("name")).lower() not in tracked), None)
    if len(oids) != 1 or field is None:
        raise ApplyError("the layer has no OID field, or no editable "
                         "attribute field that an edit can rewrite.")
    ids = call(url + "/query", {"where": edit["query"] or "1=1",
                                "returnIdsOnly": "true", "f": "json"},
               token).get("objectIds")
    if (not isinstance(ids, list) or not ids
            or not all(isinstance(i, int) for i in ids)):
        raise ApplyError("no row of the layer satisfies the replica filter.")
    pick = {"oid": oids[0], "field": field, "objectid": min(ids),
            "stamp": track.get("editDateField")
            if isinstance(track.get("editDateField"), str) else None}
    read_row(url, token, pick, pick["objectid"])
    return pick


def write_edit(edit, pick, token):
    """Rewrite the picked field with its own value, then check it landed.

    Returns (checked, text). The row is read again just before the write, so
    the value sent and the edit date compared are the current ones. Two edits
    can pick one row, and a stamp read in phase one would call the second
    edit confirmed by the first.
    """
    url = edit["target"]
    row = read_row(url, token, pick, pick["objectid"])
    before = row.get(pick["stamp"]) if pick["stamp"] else None
    update = {"attributes": {pick["oid"]: pick["objectid"],
                             pick["field"]: row[pick["field"]]}}
    data = call(url + "/applyEdits", {"updates": json.dumps([update]),
                                      "rollbackOnFailure": "true",
                                      "f": "json"}, token)
    res = data.get("updateResults")
    if (not isinstance(res, list) or len(res) != 1
            or not isinstance(res[0], dict) or res[0].get("success") is not True):
        raise ApplyError("the service did not confirm the update.")
    done = "OBJECTID %d, field %s rewritten with its own value" % (
        pick["objectid"], pick["field"])
    if not pick["stamp"]:
        return False, (done + ". Editor tracking is off on this layer, so "
                       "the edit cannot be checked here. The replica stays a "
                       "finding. Export again after the next sync.")
    after = read_row(url, token, pick, pick["objectid"]).get(pick["stamp"])
    if after is None or after == before:
        raise ApplyError("the service said success, but the last edit date "
                         "did not move. The edit may not have reached the "
                         "geodatabase.")
    return True, done + ". The last edit date moved."


def apply_plan(report, token, max_edits):
    """Make every planned edit. Returns {replica id: line} and a failure flag.

    Phase one reads every layer and row. Phase two writes. A failure in phase
    one stops the run before the first write.
    """
    edits = report["edits"]
    if len(edits) > max_edits:
        raise ApplyError("the plan holds %d edits and --max-edits is %d. "
                         "Narrow it with --only or raise the limit."
                         % (len(edits), max_edits))
    missing = [e["replica"] for e in edits if not e["target"]]
    if missing:
        raise ApplyError("no unique --targets row matches the dataset of "
                         "replica(s) %s." % ", ".join(str(i)
                                                      for i in missing))
    # One layer is one dataset. Two datasets that resolve to it would both
    # read as repaired after one edit, so the run stops before any request.
    owner = {}
    for e in edits:
        other = owner.setdefault(e["target"], e)
        if other["dataset"].upper() != e["dataset"].upper():
            raise ApplyError("replicas %d and %d plan edits on different "
                             "datasets, but --targets resolves both to one "
                             "layer. Name each dataset in full."
                             % (other["replica"], e["replica"]))
    picks = {}
    for e in edits:
        try:
            picks[e["replica"]] = preflight(e, token)
        except ApplyError as exc:
            raise ApplyError("replica %d, before any write: %s"
                             % (e["replica"], exc))
    applied, failed = {}, False
    for e in edits:
        try:
            checked, text = write_edit(e, picks[e["replica"]], token)
            applied[e["replica"]] = ("APPLIED " if checked
                                     else "UNVERIFIED ") + text
        except ApplyError as exc:
            failed = True
            applied[e["replica"]] = "FAILED " + str(exc)
    return applied, failed


# ----------------------------------------------------------------------- input

def unique_keys(pairs):
    """A JSON object's pairs as a dict. A key given twice is refused."""
    names = [k for k, _ in pairs]
    if len(set(names)) != len(names):
        raise InputError("a JSON export row has one key twice. Keep one.")
    return dict(pairs)


def check_header(kind, header):
    """Lowercase header names. Raise on a duplicate or a missing column."""
    names = [str(k).strip().lower() for k in header if k is not None]
    if len(set(names)) != len(names):
        raise InputError("%s export has two columns with one name. Keep one."
                         % kind)
    needed = REQUIRED[kind] + ((ROW_COUNT,) if kind in COUNTED else ()) + (
        (EXPORTED_AT,) if kind == "versions" else ())
    absent = [c for c in needed if c not in names]
    if absent:
        # Echo only names the tool knows. Anything else may be the first line
        # of a credential file passed by mistake.
        raise InputError("%s export has no column %s. It has %d known and %d "
                         "other column(s), not shown."
                         % (kind, ", ".join(absent),
                            len(set(names) & KNOWN_COLUMNS),
                            len(set(names) - KNOWN_COLUMNS)))
    return names


def read_rows(path, kind):
    """An export as a list of dicts with lowercase keys.

    A JSON array of objects, or a CSV. utf-8-sig, because SSMS and Excel write
    a BOM that would otherwise sit in front of the first column name. The
    header is checked even when no row follows it. A zero-byte file is what a
    failed export leaves behind.
    """
    with open(path, "r", newline="", encoding="utf-8-sig") as handle:
        text = handle.read()
    if text.lstrip()[:1] in ("[", "{"):
        rows = json.loads(text, object_pairs_hook=unique_keys)
        if not isinstance(rows, list) or not all(isinstance(r, dict)
                                                 for r in rows):
            raise InputError("%s export must be an array of objects." % kind)
    else:
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        if reader.fieldnames is None:
            raise InputError("the %s export is empty. An export needs at "
                             "least its header row." % kind)
        check_header(kind, reader.fieldnames)
    rows = [dict(zip(check_header(kind, r), [v for k, v in r.items()
                                             if k is not None]))
            for r in rows]
    counts = set(clean(r.get(ROW_COUNT)) for r in rows)
    if kind in COUNTED and rows and (
            len(counts) != 1 or cell(rows[0], ROW_COUNT, kind, 0,
                                     to_int) != len(rows)):
        raise InputError("%s export holds %d row(s), and its row_count "
                         "column does not say %d on every row. Rows were "
                         "lost or added after the SELECT ran. Export it "
                         "again, whole." % (kind, len(rows), len(rows)))
    return rows


def parse_ids(text):
    """Replica ids from '101,102'. Digits and commas only; no 'all'."""
    parts = text.split(",")
    if not all(re.match(r"^[0-9]+$", p.strip()) for p in parts):
        raise ValueError("give replica ids as digits separated by commas")
    return set(int(p) for p in parts)


# ------------------------------------------------------------------------- CLI

def _parse(argv):
    # allow_abbrev=False: a unique prefix of --apply must not write anything.
    ap = argparse.ArgumentParser(
        prog="replicakeep.py", allow_abbrev=False,
        description="Plan, then repair under --apply, an idle replica that "
                    "may hold the geodatabase compress floor.",
        epilog="Exit 0 no finding of the --exit-on level, 1 a finding, 2 "
               "the tool could not do its job. Only --apply writes: the "
               "service edits and the --report file.")
    ap.add_argument("--versions", help="the version table export")
    ap.add_argument("--replicas", help="the replica items export, with the "
                                       "Definition XML")
    ap.add_argument("--replica-log", dest="replica_log",
                    help="the replica log export")
    ap.add_argument("--targets", help="dataset to feature layer URL, CSV or "
                                      "JSON. Needed by --apply.")
    ap.add_argument("--min-idle-days", dest="min_idle_days", type=int,
                    help="plan an edit for a replica whose generation has "
                         "not moved for this many days (default %d)"
                         % DEFAULT_MIN_IDLE_DAYS)
    ap.add_argument("--silent-days", dest="silent_days", type=int,
                    help="a replica with no logged event for more than this "
                         "many days is not syncing (default %d)"
                         % DEFAULT_SILENT_DAYS)
    ap.add_argument("--event", type=int, choices=(1, 2),
                    help="the replica log event to read the generation from: "
                         "1 import, 2 export (default %d)" % DEFAULT_EVENT)
    ap.add_argument("--only", help="replica ids, comma separated: plan for "
                                   "these replicas only")
    ap.add_argument("--max-edits", dest="max_edits", type=int,
                    help="refuse an --apply run that plans more edits than "
                         "this (default %d)" % DEFAULT_MAX_EDITS)
    ap.add_argument("--as-of", dest="as_of",
                    help="measure ages from this time instead of the "
                         "exported_at time in the versions export")
    ap.add_argument("--exit-on", dest="exit_on", choices=EXIT_ON,
                    help="what makes the exit code 1: idle (default: an idle "
                         "replica not repaired, or one that cannot be judged "
                         "or moved by an edit), any (also an anchor with no "
                         "replica item), or never. A failure is exit 2 "
                         "either way.")
    ap.add_argument("--report", help="write the report to this file")
    ap.add_argument("--apply", action="store_true",
                    help="make the edits, and write --report. Without this "
                         "nothing is written and no network is used.")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def fail(message):
    print("error: %s" % message, file=sys.stderr)
    return 2


def main(argv=None):
    args = _parse(sys.argv[1:] if argv is None else argv)
    try:
        return _main(args)
    except Exception as exc:
        # An uncaught error exits 1, the code for a finding. A failure of the
        # tool is exit 2.
        return fail("%s: %s. This is a failure of the tool, not a finding."
                    % (type(exc).__name__, clean(exc)))


def _main(args):
    given = [k for k in ("versions", "replicas", "replica_log", "targets",
                         "min_idle_days", "silent_days", "event", "only",
                         "max_edits", "as_of", "exit_on", "report")
             if getattr(args, k) is not None] + ["apply"] * args.apply
    if args.self_test:
        if given:
            return fail("--self-test takes no other flag.")
        return self_test()
    absent = [k for k in ("versions", "replicas", "replica_log")
              if not getattr(args, k)]
    if absent:
        return fail("--%s is required." % ", --".join(
            a.replace("_", "-") for a in absent))
    if args.apply and not args.targets:
        return fail("--apply needs --targets.")
    if args.apply and not args.report:
        return fail("--apply needs --report, so the edits leave a record.")
    if args.report and not args.apply:
        return fail("--report is written only with --apply.")
    # A CRLF or a copied line leaves a newline on the token. http.client would
    # then raise an error that quotes the whole header.
    token = os.environ.get(TOKEN_ENV, "").strip()
    if args.apply and not all(33 <= ord(c) < 127 for c in token):
        return fail("%s holds a space, a control or a non-ASCII character. "
                    "Its value is not shown." % TOKEN_ENV)
    limits = {"min_idle_days": DEFAULT_MIN_IDLE_DAYS,
              "silent_days": DEFAULT_SILENT_DAYS,
              "max_edits": DEFAULT_MAX_EDITS}
    for key, default in limits.items():
        if getattr(args, key) is None:
            setattr(args, key, default)
        if not 0 <= getattr(args, key) <= datetime.timedelta.max.days:
            return fail("--%s must be between 0 and %d."
                        % (key.replace("_", "-"), datetime.timedelta.max.days))
    try:
        only = parse_ids(args.only) if args.only is not None else None
        as_of = parse_time(args.as_of) if args.as_of else None
    except ValueError as exc:
        return fail("%s." % exc)
    try:
        rows = dict((k, read_rows(getattr(args, k), k))
                    for k in ("versions", "replicas", "replica_log"))
        model = build(rows["versions"], rows["replicas"],
                      rows["replica_log"], as_of)
        report = analyse(model, args.min_idle_days, args.silent_days,
                         args.event or DEFAULT_EVENT, only)
        targets = read_rows(args.targets, "targets") if args.targets else []
    except (IOError, OSError, ValueError, csv.Error, InputError) as exc:
        return fail(clean(exc))
    for e in report["edits"]:
        e["target"] = resolve_target(e["dataset"], targets)

    applied, failed, repaired, handle = None, False, set(), None
    if args.apply:
        # Open the report first. A report that cannot be written must stop
        # the run before the first edit, not after the last.
        try:
            handle = open(args.report, "w", encoding="utf-8")
        except (IOError, OSError) as exc:
            return fail("could not write the report: %s" % clean(exc))
    try:
        if args.apply:
            applied, failed = apply_plan(report, token, args.max_edits)
            for e in report["edits"]:
                if applied[e["replica"]].startswith("APPLIED"):
                    repaired.update(e["covers"])
        lines = describe(report, args.min_idle_days, applied, args.apply)
        if args.apply:
            handle.write("\n".join(lines) + "\n")
    except ApplyError as exc:
        handle.write("refused before any edit: %s\n" % clean(exc))
        return fail(clean(exc))
    finally:
        if handle:
            handle.close()
    for line in lines:
        print(line)
    if args.apply:
        print("\nwrote %s" % args.report)
    else:
        print("\nCheck only. Nothing was written and no network was used.")
    if failed:
        return fail("an edit was refused or could not be checked. Read the "
                    "report.")
    return gate(report, args.exit_on or "idle", repaired)


# ------------------------------------------------------------------- self-test

AS_OF = datetime.datetime(2026, 3, 31, 12, 0, 0)


def ago(days):
    return (AS_OF - datetime.timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S")


def definition(rid, name, target=SERVER_TYPE, datasets=(), root="GPReplica"):
    """A synthetic replica item. Namespaces are declared, as real ones are."""
    body = "".join(
        '<GPReplicaDataset xsi:type="typens:GPReplicaDataset">'
        "<DatasetName>%s</DatasetName><DatasetType>%s</DatasetType>"
        "<DefQuery>%s</DefQuery></GPReplicaDataset>" % d for d in datasets)
    return ('<%s xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xmlns:typens="http://www.esri.com/schemas/ArcGIS/10.1" '
            'xsi:type="typens:GPReplica"><ID>%s</ID><Name>%s</Name>'
            "<TargetType>%s</TargetType>%s</%s>"
            % (root, rid, name, target, body, root))


FEATURE = "esriDTFeatureClass"
PLANTS = "SYNTHDB.OWNER.PLANTS"


def fixture(detached=False):
    """Synthetic exports. Item objectids differ from replica ids on purpose.

    detached adds an anchor of replica 77, which no item owns. The fixture
    holds two unreadable items, so with it every run is a finding.
    """
    reps = [
        (9001, 101, "alpha_to_web", SERVER_TYPE,
         [(PLANTS, FEATURE, "(KIND=&apos;A&apos;)")]),
        (9002, 102, "bravo_to_web", SERVER_TYPE,
         [("SYNTHDB.OWNER.PONDS", FEATURE, ""),
          (PLANTS, FEATURE, "(KIND=&apos;A&apos;)")]),
        (9003, 103, "charlie_to_web", SERVER_TYPE,
         [(PLANTS, FEATURE, "(KIND=&apos;B&apos;)")]),
        (9004, 104, "delta_to_web", SERVER_TYPE,
         [("SYNTHDB.OWNER.ROADS", "esriDTTable", "")]),
        (9005, 105, "echo_offline", "esriReplicaTargetTypeClient",
         [(PLANTS, FEATURE, "")]),
        (9006, 106, "foxtrot_to_web", SERVER_TYPE, [(PLANTS, FEATURE, "")]),
        (9007, 107, "golf_to_web", SERVER_TYPE, [(PLANTS, FEATURE, "")]),
        (9008, 108, "hotel_to_web", SERVER_TYPE,
         [("SYNTHDB.OWNER.LINKS", "esriDTRelationshipClass", "")]),
        (9009, 109, "india_to_web", SERVER_TYPE, [(PLANTS, FEATURE, "")]),
        (9010, 110, "juliet_to_web", SERVER_TYPE, [(PLANTS, FEATURE, "")]),
    ]
    replicas = [{"objectid": o, "item_type": "Replica",
                 "definition": definition(i, n, t, ds)}
                for o, i, n, t, ds in reps]
    replicas.append({"objectid": 9011, "item_type": "Sync Replica",
                     "definition": definition(
                         "-1", "sync_item", root="GPSyncReplica")})
    replicas.append({"objectid": 9012, "item_type": "Replica",
                     "definition": definition("-1", "no_id")})
    names = ["DEFAULT", "ED_WO", "SYNC_RECEIVE_REC_101_4"]
    names += ["SYNC_SEND_77_1"] * detached
    for rid in (101, 102, 103, 104, 105, 106, 107, 108, 109):
        names += ["SYNC_SEND_%d_3" % rid, "SYNC_RECEIVE_%d_3" % rid]
    versions = [{"name": n, "owner": "SDE", "state_id": 1,
                 "exported_at": ago(0)} for n in names]

    def rows(oid, plan, event=1, code=0):
        return [{"replicaid": oid, "logdate": ago(d), "event": event,
                 "errorcode": code, "targetgen": g} for d, g in plan]
    log = []
    for oid in (9001, 9002, 9003):               # idle 100 days, still syncing
        log += rows(oid, [(100, 12), (50, 12), (1, 12)])
    log += rows(9004, [(90, 5), (40, 6), (1, 6)])    # moved 40 days ago
    log += rows(9005, [(100, 1), (1, 1)])
    log += rows(9006, [(100, 1), (20, 1)])            # silent for 20 days
    log += rows(9007, [(100, 1), (1, 2)], event=2)    # only export rows
    log += rows(9007, [(60, 1)], code=7)              # and a failed import
    log += rows(9008, [(100, 4), (1, 4)])
    log += rows(9009, [(30, 3), (10, 3), (2, 4)])     # moved 2 days ago
    for r in versions:
        r["row_count"] = len(versions)
    for r in replicas:
        r["row_count"] = len(replicas)
    for r in log:
        r["row_count"] = len(log)
    return {"versions": versions, "replicas": replicas, "replica_log": log}


def write_csv(path, rows, kind=None):
    names = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as handle:
        w = csv.DictWriter(handle, fieldnames=names)
        w.writeheader()
        w.writerows(rows)


def write_fixture(folder, fx, extra=None):
    """The fixture as CSV files. Returns the argv for a plan-only run."""
    argv = []
    for kind, flag in (("versions", "--versions"), ("replicas", "--replicas"),
                       ("replica_log", "--replica-log")):
        path = os.path.join(folder, kind + ".csv")
        write_csv(path, fx[kind])
        argv += [flag, path]
    return argv


class Stub(object):
    """A local stand-in for a feature service. It records every request."""

    def __init__(self):
        self.requests = []
        self.layers = {}
        self.clock = 1000
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.route(b"")

            def do_POST(self):
                self.route(self.rfile.read(
                    int(self.headers.get("Content-Length") or 0)))

            def send(self, status, body, headers=()):
                self.send_response(status)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def route(self, body):
                parts = urllib.parse.urlsplit(self.path)
                params = dict((k, v[0]) for k, v in urllib.parse.parse_qs(
                    parts.query + "&" + body.decode("ascii")).items())
                stub.requests.append({"method": self.command,
                                      "path": parts.path, "query": parts.query,
                                      "body": body.decode("ascii"),
                                      "params": params,
                                      "token": self.headers.get(
                                          "X-Esri-Authorization")})
                base, _, leaf = parts.path.rpartition("/")
                layer = stub.layers.get(parts.path) or stub.layers[base]
                raw = layer["raw"].get(leaf)
                if raw:
                    self.send(raw[0], raw[1], raw[2])
                else:
                    self.send(200, json.dumps(stub.answer(
                        layer, leaf, params)).encode("utf-8"))

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        # A short poll interval: shutdown() waits for one, and the self-test
        # starts and stops about forty stubs.
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01})
        self.thread.daemon = True
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def answer(self, layer, leaf, params):
        rows = layer["rows"]
        if leaf == "query" and params.get("returnIdsOnly") == "true":
            ids = layer["ids"].get(params["where"], sorted(rows))
            return {"objectIdFieldName": "OBJECTID", "objectIds": ids}
        if leaf == "query":
            keep = params["outFields"].split(",")
            return {"features": [{"attributes": dict(
                (k, v) for k, v in rows[int(i)].items() if k in keep)}
                for i in params["objectIds"].split(",")
                if int(i) in rows]}
        if leaf == "applyEdits":
            for u in json.loads(params["updates"]):
                rows[u["attributes"]["OBJECTID"]].update(u["attributes"])
                layer["writes"] += 1
                if layer["tracking"] and (layer["freeze_after"] is None
                                          or layer["writes"]
                                          <= layer["freeze_after"]):
                    self.clock += 1
                    rows[u["attributes"]["OBJECTID"]]["EDITED"] = (
                        None if layer["tracking"] == "null" else self.clock)
            return layer["apply"] or {"updateResults": [
                {"objectId": 1, "success": True}]}
        return layer["info"]

    def add(self, name, tracking=True, **info):
        meta = {"capabilities": "Query,Update,Editing",
                "fields": [
                    {"name": "OBJECTID", "type": "esriFieldTypeOID",
                     "editable": True},
                    {"name": "SHAPE", "type": "esriFieldTypeGeometry",
                     "editable": True},
                    {"name": "GLOBALID", "type": "esriFieldTypeGlobalID",
                     "editable": True},
                    {"name": "EDITOR", "type": "esriFieldTypeString",
                     "editable": True},
                    {"name": "EDITED", "type": "esriFieldTypeDate",
                     "editable": True},
                    {"name": "GUIDX", "type": "esriFieldTypeGUID",
                     "editable": True},
                    {"name": "BLOBX", "type": "esriFieldTypeBlob",
                     "editable": True},
                    {"name": "RASTERX", "type": "esriFieldTypeRaster",
                     "editable": True},
                    {"name": "XMLX", "type": "esriFieldTypeXML",
                     "editable": True},
                    {"name": "NOEDIT", "type": "esriFieldTypeString",
                     "editable": False},
                    {"name": "LABEL", "type": "esriFieldTypeString",
                     "editable": True}],
                "isDataBranchVersioned": False}
        if tracking:
            meta["editFieldsInfo"] = {"editorField": "EDITOR",
                                      "editDateField": "EDITED"}
        meta.update(info)
        self.layers["/arcgis/rest/services/%s/FeatureServer/0" % name] = {
            "info": meta, "tracking": tracking, "apply": None, "raw": {},
            "writes": 0, "freeze_after": None, "ids": {}, "rows": {
                7: {"OBJECTID": 7, "EDITOR": "a", "EDITED": 1, "LABEL": "x"},
                9: {"OBJECTID": 9, "EDITOR": "b", "EDITED": 2, "LABEL": "y"}}}
        return self.base + "/arcgis/rest/services/%s/FeatureServer/0" % name

    def layer(self, name):
        return self.layers["/arcgis/rest/services/%s/FeatureServer/0" % name]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Harness(object):
    """The check() and raises() of the self-test, kept apart so it can be tested."""

    def __init__(self):
        self.passed = [0]
        self.failed = []

    def total(self):
        return self.passed[0] + len(self.failed)

    def check(self, cond, label):
        if cond:
            self.passed[0] += 1
            print("PASS  %s" % label)
        else:
            self.failed.append(label)
            print("FAIL  %s" % label)

    def raises(self, fn, label, exc_type=InputError):
        try:
            fn()
        except exc_type:
            self.check(True, label)
        except Exception as exc:
            self.check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            self.check(False, "%s (no error raised)" % label)


def self_test():
    """Assertions over the decision core and the writes. No real service."""
    harness = Harness()
    check, raises = harness.check, harness.raises
    failed = harness.failed

    def run(argv, env=None):
        """main() in process: (exit code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        saved = dict(os.environ)
        os.environ.pop(TOKEN_ENV, None)
        if env is not None:
            os.environ[TOKEN_ENV] = env
        try:
            with contextlib.redirect_stdout(out),                     contextlib.redirect_stderr(err):
                code = main(argv)
        finally:
            os.environ.clear()
            os.environ.update(saved)
        return code, out.getvalue(), err.getvalue()

    def message(fn, exc_type=InputError):
        """The text of the error fn raises. A missing error gives text that
        no assertion on a message can match, so it fails instead of
        vanishing."""
        text = "no error was raised"
        try:
            fn()
        except exc_type as exc:
            text = str(exc)
        return text

    print("replicakeep self-test: no database, no real service, no credentials")
    print("-" * 68)

    # ---- the decision core, over canned rows
    fx = fixture()
    model = build(**fx)
    report = analyse(model)
    dreport = analyse(build(**fixture(True)))
    by = dict((v["id"], v) for v in report["verdicts"])
    edits = dict((e["replica"], e) for e in report["edits"])
    check(model["as_of"] == AS_OF, "the as-of time is the export's database "
          "clock, not the local clock")
    check(by[101]["status"] == IDLE and by[101]["edit"]["covers"] == [101, 102],
          "a replica idle 100 days that still syncs is IDLE")
    check("holds 3 anchor version(s)" in by[101]["detail"]
          and "SYNC_RECEIVE_REC_101_4" in model["anchors"][101],
          "a SYNC_RECEIVE_REC_ version is an anchor of its replica")
    check(by[102]["status"] == COVERED and by[102]["edit"] is edits[101],
          "a replica on the same dataset and filter shares one edit")
    check("100 day(s)" in by[101]["detail"],
          "the idle time is measured from the first log row of the run of "
          "equal generations")
    check(by[103]["status"] == IDLE and 103 in edits,
          "the same dataset with another filter gets its own edit  "
          "<-- pinned defect")
    check(by[104]["status"] == IDLE and "40 day(s)" in by[104]["detail"]
          and "generation 6 unchanged" in by[104]["detail"],
          "idle time runs from the LAST change of generation, not the first "
          "row  <-- pinned defect")
    check(edits[104]["dataset"] == "SYNTHDB.OWNER.ROADS",
          "a table is a dataset an edit can use")
    check(by[105]["status"] == CLIENT, "a client-type replica is never "
          "edited  <-- pinned defect")
    check(by[106]["status"] == NOT_SYNCING and "20" not in by[106]["detail"]
          and 106 not in edits, "a replica silent for 20 days gets no edit, "
          "and is not told it is idle  <-- pinned defect")
    check(by[107]["status"] == NO_ACK, "a replica with only export rows and "
          "a failed import has no known generation")
    check(by[108]["status"] == NO_DATASET and 108 not in edits,
          "a replica that lists no feature class or table gets no edit")
    check(by[109]["status"] == ACTIVE, "a generation that moved 2 days ago "
          "is ACTIVE, though the first row is 30 days old  <-- pinned defect")
    check(by[110]["status"] == NO_ANCHOR,
          "a replica that holds no anchor pins nothing and is left alone")
    check(dreport["detached"] == [77] and dreport["unread"] == 2
          and report["detached"] == [],
          "an anchor with no replica item is reported, and a Sync Replica "
          "item and an item with ID -1 are counted as unread")
    check(len(edits) == 3 and len([v for v in report["verdicts"]
                                   if v["status"] in (IDLE, COVERED)]) == 4,
          "four idle replicas plan three edits")
    check(by[101]["edit"]["query"] == "(KIND='A')",
          "an escaped filter in the XML is read as plain text")
    ev2 = dict((v["id"], v["status"])
               for v in analyse(model, event=2)["verdicts"])
    check(ev2[107] == ACTIVE and ev2[101] == NO_ACK,
          "--event 2 reads the export rows instead of the import rows")
    edge = dict((v["id"], v["status"]) for v in analyse(
        model, min_idle_days=100)["verdicts"])
    check(edge[101] == IDLE and edge[104] == ACTIVE,
          "a replica idle exactly the limit is IDLE, one day under is not")
    only = dict((v["id"], v["status"]) for v in analyse(
        model, only=set([109]))["verdicts"])
    check(only[109] == ACTIVE and only[101] == EXCLUDED,
          "--only leaves every other replica out of the plan")
    wide = analyse(model, silent_days=30)
    check(dict((v["id"], v["status"]) for v in wide["verdicts"])[106] == IDLE,
          "a wider silence limit lets a replica that synced 20 days ago "
          "be planned")

    raises(lambda: analyse(model, only=set([999])),
           "--only with an id no replica item carries is refused, never an "
           "all-clear for nothing  <-- pinned defect")

    # ---- cells, times, definitions
    check(parse_time("2026-03-31") == datetime.datetime(2026, 3, 31),
          "a bare date is midnight")
    check(parse_time("2026-03-31T01:02:03.5Z")
          == datetime.datetime(2026, 3, 31, 1, 2, 3),
          "a T separator, a fraction and a zone are accepted")
    check(parse_time("  ") is None, "a blank time is None")
    raises(lambda: parse_time("03:00:00 PM"),
           "a 12-hour time is refused, never read as 03:00  "
           "<-- pinned defect", ValueError)
    raises(lambda: parse_time("2026-03-31 03:00:00 PM"),
           "a date with a 12-hour time is refused, never read as 03:00  "
           "<-- pinned defect", ValueError)
    raises(lambda: parse_time("2026-13-01"), "month 13 is refused",
           ValueError)
    check(to_int("17.0") == 17 and to_int(" 5 ") == 5, "17.0 is 17")
    raises(lambda: to_int("1.5"), "1.5 is not a whole number", ValueError)
    raises(lambda: to_int("inf"), "inf is refused as ValueError, not "
           "OverflowError  <-- pinned defect", ValueError)
    check(clean("a\nVERDICT: clean") == "a\\nVERDICT: clean",
          "a newline in a name cannot forge a report line  "
          "<-- pinned defect")
    text = message(lambda: cell({"c": "SECRET-VALUE"}, "c", "replicas", 3,
                                to_int))
    check("SECRET-VALUE" not in text and "row 4" in text,
          "a bad cell names the row and never echoes the value  "
          "<-- pinned defect")
    check(parse_definition("   ") is None, "a blank definition is unread")
    check(parse_definition(definition("x1", "n")) is None,
          "a non-numeric replica ID is unread")
    check(parse_definition(definition(0, "n"))["id"] == 0
          and parse_definition(definition(-1, "n")) is None,
          "replica ID 0 is read and -1 is not")
    check(parse_definition(definition(5, "")) ["name"] == "(unnamed)",
          "a replica with no name is called (unnamed)")
    check(parse_definition(definition(5, "n", root="GPSyncReplica")) is None,
          "a Sync Replica root is unread even when it carries a usable ID")
    raises(lambda: parse_definition("<!DOCTYPE a [<!ENTITY b 'c'>]><a/>"),
           "a definition with a DTD is refused  <-- pinned defect")
    raises(lambda: parse_definition("<GPReplica><ID>"),
           "a definition that is not XML is refused")
    check(parse_definition('<GPReplica><ID>4</ID><GPReplicaDataset>'
                           '<DatasetType>esriDTTable</DatasetType>'
                           '</GPReplicaDataset></GPReplica>')["datasets"] == [],
          "a dataset with no name is skipped")

    # ---- build() refusals
    def made(**changes):
        f = fixture()
        f.update(changes)
        return build(**f)
    raises(lambda: made(versions=[]), "an empty versions export is refused")
    raises(lambda: made(replicas=[]), "an empty replicas export is refused")
    stale = fixture()["versions"]
    stale[-1]["exported_at"] = ago(5)
    raises(lambda: made(versions=stale),
           "two exported_at times in one export are refused")
    blank = fixture()["versions"]
    for r in blank:
        r["exported_at"] = ""
    raises(lambda: made(versions=blank), "a blank exported_at is refused")
    twin = fixture()["replicas"]
    twin[1]["objectid"] = twin[0]["objectid"]
    raises(lambda: made(replicas=twin), "one item objectid twice is refused")
    same = fixture()["replicas"]
    same[1]["definition"] = same[0]["definition"]
    raises(lambda: made(replicas=same),
           "one replica ID on two items is refused  <-- pinned defect")
    nolog = fixture()["replica_log"]
    nolog[0]["logdate"] = ""
    raises(lambda: made(replica_log=nolog), "a blank logdate is refused")
    raises(lambda: build(**dict(fixture(), as_of=AS_OF
                                - datetime.timedelta(days=2))),
           "an as-of time before the newest log row is refused, never a "
           "negative age  <-- pinned defect")
    check(build(as_of=AS_OF - datetime.timedelta(days=1), **fixture())
          ["as_of"] == AS_OF - datetime.timedelta(days=1),
          "an as-of time equal to the newest log row is allowed")
    later = build(as_of=AS_OF + datetime.timedelta(days=1), **fixture())
    check("101 day(s)" in analyse(later)["verdicts"][0]["detail"],
          "an as-of override moves every age")
    raises(lambda: made(replica_log=[]),
           "an empty replica log is refused, never read as no replica "
           "syncing  <-- pinned defect")
    raises(lambda: made(replica_log=[
        dict(r, replicaid=r["replicaid"] - 8900)
        for r in fixture()["replica_log"]]),
           "a log keyed by replica ID, which joins to no item, is refused  "
           "<-- pinned defect")
    raises(lambda: made(replica_log=[r for r in fixture()["replica_log"]
                                     if r["replicaid"] == 9005]),
           "a log that holds rows only for a client replica judges no "
           "server replica and is refused")
    raises(lambda: made(replicas=[r for r in fixture()["replicas"]
                                  if r["objectid"] in (9011, 9012)]),
           "anchors with no readable replica item are refused  "
           "<-- pinned defect")

    # ---- a replica log cut to other shapes, one replica at a time
    def lr(oid, days, gen, event=1, code=0):
        return {"replicaid": oid, "logdate": ago(days), "event": event,
                "errorcode": code, "targetgen": gen}

    def relog(drop, extra, **limits):
        f = fixture()
        f["replica_log"] = [r for r in f["replica_log"]
                            if r["replicaid"] not in drop] + extra
        for r in f["replica_log"]:
            r["row_count"] = len(f["replica_log"])
        return dict((v["id"], v)
                    for v in analyse(build(**f), **limits)["verdicts"])
    failing = relog((9004,), [lr(9004, 90, 5), lr(9004, 40, 6)]
                    + [lr(9004, d, 6, code=9) for d in (5, 4, 3, 2, 1)])
    check(failing[104]["status"] == NOT_SYNCING and failing[104]["edit"] is None
          and "successful" in failing[104]["detail"],
          "a replica whose recent syncs all fail is NOT_SYNCING, not IDLE, "
          "and gets no edit  <-- pinned defect")
    check(relog((9003,), [])[103]["status"] == NOT_SYNCING,
          "a replica that holds anchors and has no log row is NOT_SYNCING")
    check(relog((9003,), [lr(9003, 1, 12, code=5)])[103]["status"]
          == NOT_SYNCING,
          "a replica whose only log rows are failures is NOT_SYNCING")
    edge3 = [lr(9003, 100, 12), lr(9003, 3, 12)]
    check(relog((9003,), edge3)[103]["status"] == IDLE
          and relog((9003,), edge3, silent_days=2)[103]["status"]
          == NOT_SYNCING,
          "a replica whose last good sync was exactly --silent-days ago "
          "still syncs; one day more and it does not  <-- pinned defect")
    # TargetType: only Client is a client. Undefined, absent and a misspelling
    # are unjudged, never a clean exit.
    for odd in ("esriReplicaTargetTypeUndefined", "",
                "esriReplicaTargetTypeserver"):
        typed = fixture()
        typed["replicas"][2]["definition"] = definition(
            103, "c", target=odd, datasets=[(PLANTS, FEATURE, "")])
        found = dict((v["id"], v) for v in analyse(build(**typed))["verdicts"])
        check(found[103]["status"] == NO_TYPE and found[103]["edit"] is None
              and gate(analyse(build(**typed)), "idle", repaired=set(
                  [101, 102, 104, 106, 107, 108])) == 1,
              "a replica whose TargetType is %r is NO_TYPE and a finding, "
              "not a client and not a clean exit  <-- pinned defect"
              % odd)
    short = relog((9003,), [lr(9003, 3, 12)])
    check(short[103]["status"] == SHORT_LOG and short[103]["edit"] is None
          and "unknown" in short[103]["detail"],
          "a log that covers 3 days with one generation does not say the "
          "replica moved 3 days ago  <-- pinned defect")
    check(relog((9003,), [lr(9003, 3, 12)], min_idle_days=3)[103]["status"]
          == IDLE
          and "at least 100 day(s)" in relog((9003,), [lr(9003, 100, 12),
                                                       lr(9003, 1, 12)]
                                             )[103]["detail"],
          "a log of exactly --min-idle-days with one generation is IDLE, "
          "and an unchanged run is worded as a lower bound")
    backwards = fixture()
    backwards["replica_log"].reverse()
    check([(v["id"], v["status"]) for v in analyse(build(**backwards))
           ["verdicts"]] == [(v["id"], v["status"])
                             for v in report["verdicts"]],
          "log rows in any order give the same verdicts")
    swapped = fixture()
    swapped["replicas"][0]["definition"] = definition(
        101, "a", datasets=[("DB.OWNER.PONDS", FEATURE, ""),
                            (PLANTS, FEATURE, "(KIND=&apos;A&apos;)")])
    swapped["replicas"][1]["definition"] = definition(
        102, "b", datasets=[(PLANTS, FEATURE, "(KIND=&apos;A&apos;)")])
    lower = fixture()
    lower["replicas"][1]["definition"] = definition(
        102, "b", datasets=[(PLANTS.lower(), FEATURE, "(KIND=&apos;A&apos;)")])
    check(dict((v["id"], v["status"]) for v in analyse(build(**lower))
               ["verdicts"])[102] == COVERED,
          "two replicas whose dataset names differ only in case share one "
          "edit")
    sw = analyse(build(**swapped))
    check([(v["status"], v["edit"]["dataset"]) for v in sw["verdicts"][:2]]
          == [(IDLE, "DB.OWNER.PONDS"), (IDLE, PLANTS)],
          "an edit uses the first dataset only, so a replica that lists "
          "that dataset second is not covered by another's first")

    # ---- the other pure functions
    check(parse_ids("101, 102") == set([101, 102]), "replica ids parse")
    for bad in ("all", "", "1,,2", "-1"):
        raises(lambda b=bad: parse_ids(b),
               "the id list %r is refused; there is no 'all'" % bad,
               ValueError)
    targets = [{"dataset": "OWNER.PLANTS", "layer_url": "u1"},
               {"dataset": "roads", "layer_url": "u2"},
               {"dataset": "PLANTS", "layer_url": "u3"}]
    check(resolve_target("DB.OWNER.ROADS", targets) == "u2",
          "a target matches by its trailing parts, without case")
    check(resolve_target("DB.OWNER.PLANTS", targets) is None,
          "two targets that match one dataset resolve to none, not the "
          "first  <-- pinned defect")
    check(resolve_target("db.owner.roads", targets[1:2]) == "u2"
          and resolve_target("db.owner.roads", [dict(
              targets[1], dataset="ROADS")]) == "u2",
          "a dataset name matches a target without regard to case")
    check(resolve_target("DB.OWNER.PONDS", targets) is None,
          "a dataset with no target resolves to none")
    check(resolve_target("DB.OWNER.XPLANTS", targets[2:]) is None,
          "a target matches whole name parts, not the end of a word  "
          "<-- pinned defect")
    check((gate(report, "idle"), gate(report, "any"), gate(report, "never"))
          == (1, 1, 0), "gate: idle and any find the plan, never does not")
    four = analyse(model, only=set([101, 102, 103, 104]))
    check((gate(four, "idle"), gate(four, "idle", repaired=set(
        [101, 102, 103, 104]))) == (1, 0),
          "a replica whose edit was applied is no longer a finding")
    quiet = analyse(model, only=set([109]))
    check(gate(quiet, "idle") == 0, "nothing idle is exit 0")
    quiet["detached"], quiet["unread"] = [77], 0
    check((gate(quiet, "idle"), gate(quiet, "any")) == (0, 1),
          "an anchor with no replica is a finding only under --exit-on any")
    quiet["unread"] = 1
    check((gate(quiet, "idle"), gate(quiet, "any"), gate(quiet, "never"))
          == (1, 1, 0), "an anchor with no replica item is a finding when "
          "another item could not be read, for that item may own it  "
          "<-- pinned defect")
    quiet["detached"] = []
    check(gate(quiet, "idle") == 0, "an unread item with no detached anchor "
          "is no finding")
    quiet["odd"] = 2
    check((gate(quiet, "idle"), gate(quiet, "never")) == (1, 0),
          "a version name that looks like an anchor and matches no pattern "
          "is a finding  <-- pinned defect")
    quiet["odd"] = 0
    for status in (IDLE, COVERED, NOT_SYNCING, NO_ACK, NO_DATASET, NO_TYPE,
                   SHORT_LOG):
        quiet["verdicts"][0]["status"] = status
        check((gate(quiet, "idle"), gate(quiet, "any"), gate(quiet, "never"))
              == (1, 1, 0),
              "a %s replica is a finding by default, never a clean exit  "
              "<-- pinned defect" % status)
    for status in (ACTIVE, CLIENT, NO_ANCHOR, EXCLUDED):
        quiet["verdicts"][0]["status"] = status
        check(gate(quiet, "any") == 0,
              "a %s replica is no finding" % status)
    for url, what in (("https://h/x/FeatureServer/0?token=a", "a query"),
                      ("https://h/x/FeatureServer/0#a", "a fragment"),
                      ("https://user@h/x/FeatureServer/0", "a user name"),
                      ("https://:pw@h/x/FeatureServer/0", "a password")):
        check(holds_secret(url), "a URL with %s holds a secret" % what)
    check(not holds_secret("https://h/x/FeatureServer/0"),
          "a plain layer URL holds no secret")
    text = message(lambda: apply_plan(report, "", 3), ApplyError)
    check("--max-edits" not in text and "--targets" in text,
          "a plan of exactly --max-edits edits is not refused for its "
          "size  <-- pinned defect")
    lines = describe(report, 6)
    text = "\n".join(lines)
    check(lines[-1].startswith("VERDICT: 4 idle replica(s) at 6 day(s)")
          and "3 more hold anchors" in lines[-1]
          and "Nothing was written" in lines[-1],
          "the report ends with a VERDICT line that counts the replicas it "
          "could not judge")
    check("ANCHOR WITHOUT" not in text and "version name(s)" not in text
          and "no readable item" not in lines[-1],
          "a clean model prints no detached-anchor or odd-name line")
    dlines = describe(dreport, 6)
    dtext = "\n".join(dlines)
    check("2 item(s) were not a usable GPReplica and one may own these"
          in dtext and "No edit helps" not in dtext
          and "1 anchor replica id(s) have no readable item" in dlines[-1],
          "a detached anchor beside unread items is called unjudged in the "
          "report and the VERDICT line  <-- pinned defect")
    nounread = dict(dreport, unread=0)
    check("No edit helps these" in "\n".join(describe(nounread, 6))
          and "no readable item" not in describe(nounread, 6)[-1],
          "a detached anchor with every item read is called beyond an edit")
    odd = fixture(True)
    odd["versions"] += [dict(odd["versions"][0], name=n) for n in (
        "Sde.SYNC_SEND_101_3", "DB.SDE.SYNC_SEND_102_3", "SYNC_SEND_abc",
        "X_SYNC_SEND_1_1", "my_sync_test")]
    omodel = build(**odd)
    oreport = analyse(omodel)
    otext = describe(oreport, 6)
    check(omodel["odd"] == 3 and "Sde.SYNC_SEND_101_3" in omodel["anchors"][101]
          and "DB.SDE.SYNC_SEND_102_3" in omodel["anchors"][102]
          and oreport["odd"] == 3 and "3 version name(s) hold SYNC_" in
          "\n".join(otext) and "3 SYNC_ version name(s) match no anchor "
          "pattern" in otext[-1],
          "an owner-qualified anchor name is an anchor, and a SYNC_ name that "
          "fits no pattern is counted and reported  <-- pinned defect")
    qd = describe(dict(report, edits=[dict(report["edits"][0],
                                           query="a\nb")]), 6)
    check("filter a\\nb" in "\n".join(qd)
          and not any(l.startswith("b") for l in qd),
          "a newline in a replica filter cannot forge a report line  "
          "<-- pinned defect")
    check("This is an observation" in text, "the report words the claim as "
          "an observation")
    check("hold no anchor" in text and "ANCHOR WITHOUT A REPLICA ITEM"
          in dtext and "compressfloor" in dtext,
          "an anchor with no replica points the reader to compressfloor")
    check("replica 101" in text and "NOT_SYNCING" in text,
          "every shown replica gets one line")
    check("target: none" in text and "covers 101, 102" in text,
          "the plan names the replicas one edit covers")
    check("  edit 2 of 3 for replica 103 (covers 103): dataset "
          "SYNTHDB.OWNER.PLANTS, filter (KIND='B')" in text
          and "dataset SYNTHDB.OWNER.ROADS, filter none" in text,
          "the plan numbers each edit and prints its filter, or none")
    check("generation 4 moved 2 day(s) ago" in by[109]["detail"],
          "the detail line shows the newest generation, not the one before")
    check((DEFAULT_MIN_IDLE_DAYS, DEFAULT_SILENT_DAYS, DEFAULT_MAX_EDITS,
           DEFAULT_EVENT) == (6, 3, 10, 1),
          "the shipped defaults are the ones the README table lists")
    check(describe(analyse(model, only=set([109])), 6)[-3] == "  no edit "
          "planned", "an empty plan says so")

    with tempfile.TemporaryDirectory() as tmp:
        def path(name):
            return os.path.join(tmp, name)

        def put(name, text, mode="w"):
            with open(path(name), mode, **(
                    {"encoding": "utf-8", "newline": ""} if mode == "w"
                    else {})) as handle:
                handle.write(text)
            return path(name)

        # ---- read_rows
        argv = write_fixture(tmp, fx)
        rows = read_rows(path("replicas.csv"), "replicas")
        check(len(rows) == 12 and rows[0]["objectid"] == "9001",
              "a CSV export is read, with lowercase keys")
        put("bom.csv", b"\xef\xbb\xbfname,exported_at,row_count\r\n"
            b"v,2026-03-31 12:00:00,1\r\n", "wb")
        check(read_rows(path("bom.csv"), "versions")[0]["name"] == "v",
              "a UTF-8 byte order mark does not hide the first column")
        put("upper.csv", "NAME,Exported_At,ROW_COUNT\nv,2026-03-31,1\n")
        check(read_rows(path("upper.csv"), "versions")[0]["name"] == "v",
              "column names match without regard to case")
        put("rows.json", json.dumps(fx["replica_log"]))
        check(len(read_rows(path("rows.json"), "replica_log"))
              == len(fx["replica_log"]), "a JSON array of objects is read")
        put("empty.json", "[]")
        check(read_rows(path("empty.json"), "replica_log") == [],
              "a JSON [] is zero rows")
        put("hdr.csv", "replicaid,logdate,event,errorcode,targetgen,"
            "row_count\n")
        check(read_rows(path("hdr.csv"), "replica_log") == [],
              "a header-only CSV is zero rows")
        put("t.csv", "dataset,layer_url\nA,https://h/x/FeatureServer/0\n")
        check(read_rows(path("t.csv"), "targets")[0]["dataset"] == "A",
              "a targets file needs no row_count")
        for name, text, label in (
                ("zero.csv", "", "a zero-byte file"),
                ("short.csv", "name,exported_at,row_count\nv,2026-01-01,2\n",
                 "a row_count that does not match the rows read"),
                ("nocount.csv", "name,exported_at\nv,2026-01-01\n",
                 "an export with no row_count column"),
                ("nostamp.csv", "name,row_count\nv,1\n",
                 "a versions export with no exported_at column"),
                ("twice.csv", "name,NAME,exported_at,row_count\n"
                 "a,b,2026-01-01,1\n", "two columns with one name"),
                ("arr.json", '{"a": 1}', "a JSON object that is not an array"),
                ("dup.json", '[{"a": 1, "a": 2}]', "a JSON key given twice"),
                ("mixed.csv", "name,exported_at,row_count\nv,2026-01-01,2\n"
                 "w,2026-01-01,3\n", "two different row_count values")):
            put(name, text)
            raises(lambda n=name: read_rows(path(n), "versions"),
                   "%s is refused" % label)
        put("secret.csv", "hunter2-password,exported_at,row_count\n")
        text = message(lambda: read_rows(path("secret.csv"), "versions"))
        check("hunter2" not in text and "no error" not in text,
              "an unknown header is never echoed: a credential file "
              "passed by mistake stays private  <-- pinned defect")
        raises(lambda: read_rows(path("hdr.csv"), "versions"),
               "an export of the wrong kind is refused by its columns")

        # ---- the command line, plan only
        before = sorted(os.listdir(tmp))
        code, out, err = run(argv)
        check(code == 1 and "VERDICT: 4 idle replica(s)" in out
              and "Check only" in out, "a plan-only run finds idle replicas "
              "and exits 1")
        check(sorted(os.listdir(tmp)) == before and err == "",
              "a plan-only run writes no file")
        check(run(argv + ["--exit-on", "never"])[0] == 0,
              "--exit-on never exits 0 on the same findings")
        check(run(argv + ["--only", "109"])[0] == 0,
              "--only 109 plans nothing and exits 0")
        code, out, err = run(argv + ["--min-idle-days", "200"])
        check(code == 1 and "VERDICT: 0 idle replica(s)" in out
              and "6 more hold anchors" in out,
              "a limit above every idle time leaves no idle replica, but "
              "the replicas that cannot be judged still make exit 1  "
              "<-- pinned defect")
        check(run(argv + ["--as-of", "2026-04-01", "--min-idle-days", "0",
                          "--silent-days", "400"])[0] == 1,
              "--as-of is read as a date")
        for flags, label in (
                (["--as-of", "yesterday"], "a bad --as-of"),
                (["--only", "all"], "--only all"),
                (["--min-idle-days", "-1"], "a negative limit"),
                (["--silent-days", "99999999999"], "a limit over timedelta"),
                (["--apply"], "--apply with no --targets"),
                (["--targets", path("t.csv"), "--apply"],
                 "--apply with no --report"),
                (["--report", path("r.txt")],
                 "--report with no --apply"),
                (["--as-of", "2026-03-01"], "an --as-of before the log"),
                (["--only", "999"], "--only with an unknown replica id")):
            code, out, err = run(argv + flags)
            check(code == 2 and err.startswith("error:"),
                  "%s exits 2 on stderr" % label)
        check(not os.path.exists(path("r.txt")),
              "a refused --report was never created")
        check("--apply needs --targets" in run(argv + ["--apply"])[2]
              and "--apply needs --report" in run(
                  argv + ["--targets", path("t.csv"), "--apply"])[2],
              "--apply names the flag it is missing")
        code, out, err = run(argv[:2])
        check(code == 2 and "--replicas" in err and "--replica-log" in err,
              "a missing export is named")
        code, out, err = run(["--versions", path("nope.csv")] + argv[2:])
        check(code == 2, "an unreadable export exits 2, not 1")
        code, out, err = run(argv[:5] + [path("hdr.csv")])
        check(code == 2 and "no row for any replica" in err,
              "a header-only replica log exits 2, never a clean 0  "
              "<-- pinned defect")
        code, out, err = run(["--self-test", "--apply"])
        check(code == 2 and "takes no other flag" in err,
              "--self-test refuses every other flag")
        # Each argv is valid if the prefix were accepted, so only the parser's
        # allow_abbrev=False can make it exit 2.
        for abbrev in (["--ap"], ["--repor", "x"], ["--replica-l", "x"],
                       ["--exit", "any"], ["--vers", "x"],
                       ["--min-idle", "5"], ["--sil", "5"]):
            with contextlib.redirect_stderr(io.StringIO()):
                refused = False
                try:
                    _parse(abbrev)
                except SystemExit as exc:
                    refused = exc.code == 2
            check(refused, "the abbreviation %s is refused by the parser  "
                  "<-- pinned defect" % abbrev[0])
        fx2 = fixture()
        fx2["replicas"][0]["definition"] = "<GPReplica><ID>"
        write_csv(path("replicas.csv"), fx2["replicas"])
        code, out, err = run(argv)
        check(code == 2 and "not XML" in err,
              "a broken definition exits 2, not 1, and not a traceback")
        write_csv(path("replicas.csv"), fx["replicas"])
        code, out, err = run(argv + ["--exit-on", "never"], env=None)
        check(code == 0 and "\nPLAN\n" in out, "the plan prints")
        fx3 = fixture()
        fx3["replicas"][1]["definition"] = definition(
            102, "bad\nVERDICT: 0 idle", datasets=[(PLANTS, FEATURE, "")])
        write_csv(path("replicas.csv"), fx3["replicas"])
        code, out, err = run(argv)
        check(not any(l.startswith("VERDICT: 0 idle")
                      for l in out.split("\n"))
              and "bad\\nVERDICT: 0 idle" in out,
              "a replica name with a newline cannot forge a VERDICT line  "
              "<-- pinned defect")
        write_csv(path("replicas.csv"), fx["replicas"])

        # ---- a plan-only run uses no network
        real_open = OPENER.open
        OPENER.open = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("network used"))
        try:
            put("tg.csv", "dataset,layer_url\nPLANTS,https://h.example/x/"
                "FeatureServer/0\n")
            code, out, err = run(argv + ["--targets", path("tg.csv")])
            check(code == 1 and "target: https://h.example/x/FeatureServer/0"
                  in out and "target: none" in out,
                  "a plan-only run shows the targets it resolved and opens "
                  "no connection")
            put("tg.json", json.dumps([{
                "dataset": "PLANTS",
                "layer_url": "https://h.example/x/FeatureServer/0"}]))
            code, out, err = run(argv + ["--targets", path("tg.json")])
            check(code == 1 and "target: https://h.example/x/FeatureServer/0"
                  in out, "a JSON targets file resolves like a CSV one")
        finally:
            OPENER.open = real_open

        # ---- --apply against a local stub of a feature service
        secret = "tok-SECRET-4f9a"

        def setup(tracking=True):
            stub = Stub()
            plants = stub.add("plants", tracking)
            roads = stub.add("roads", tracking)
            stub.layer("plants")["ids"]["(KIND='A')"] = [9]
            put("tg2.csv", "dataset,layer_url\nOWNER.PLANTS,%s\nROADS,%s\n"
                % (plants, roads))
            return stub

        def apply_args(extra=(), full=False):
            # Unless full, only the four idle replicas are in the run, so a
            # repaired run is exit 0 and not 1 for the three left unjudged.
            return argv + ["--targets", path("tg2.csv"), "--report",
                           path("rep.txt"), "--apply"] + (
                [] if full else ["--only", "101,102,103,104"]) + list(extra)

        def go(stub, extra=(), env=secret, full=False):
            try:
                result = run(apply_args(extra, full), env=env)
                with open(path("rep.txt"), encoding="utf-8") as handle:
                    text = handle.read()
            finally:
                stub.close()
            return result + (text,)

        def writes(stub):
            return [r for r in stub.requests
                    if r["path"].endswith("/applyEdits")]

        stub = setup()
        code, out, err, text = go(stub)
        sent = writes(stub)
        updates = [json.loads(r["params"]["updates"]) for r in sent]
        check(code == 0 and text.count("APPLIED") == 3
              and "VERDICT: 4 idle replica(s)" in text
              and "Every edit was made and checked" in text,
              "--apply makes the three planned edits, reports them, and "
              "exits 0 because every idle replica was repaired")
        check(all(r["token"] == "Bearer " + secret for r in stub.requests),
              "every request carries the token in the X-Esri-Authorization "
              "header")
        check(not any(secret in (r["path"] + r["query"] + r["body"])
                      for r in stub.requests)
              and secret not in out + err + text,
              "the token is in no URL, no request body, no output and no "
              "report file  <-- pinned defect")
        check([u[0]["attributes"]["OBJECTID"] for u in updates] == [9, 7, 7]
              and stub.requests[0]["params"].get("where") is None
              and any(r["params"].get("where") == "(KIND='A')"
                      for r in stub.requests),
              "each edit rewrites the lowest OBJECTID that satisfies the "
              "replica's own filter, sent as written")
        check(all(list(u[0]) == ["attributes"] and len(u) == 1
                  and sorted(u[0]["attributes"]) == ["LABEL", "OBJECTID"]
                  for u in updates)
              and stub.layer("plants")["rows"][9]["LABEL"] == "y"
              and stub.layer("plants")["rows"][7]["LABEL"] == "x",
              "an edit sends no geometry and no tracking field, and writes "
              "back the value the row already held  <-- pinned defect")
        first = min(i for i, r in enumerate(stub.requests)
                    if r["path"].endswith("/applyEdits"))
        check(first == 10 and len(sent) == 3 and all(
            r["method"] == "GET" or r["path"].endswith("/query")
            for r in stub.requests[:first]),
              "every layer and row is read before the first write, and the "
              "first row is read once more just before it")
        check(all(r["params"].get("rollbackOnFailure") == "true"
                  for r in sent),
              "every edit asks the service to roll back on failure  "
              "<-- pinned defect")
        check(stub.requests[0]["method"] == "GET"
              and not any("KIND" in r["query"] for r in stub.requests),
              "a filter never travels in a URL")
        check(stub.layer("plants")["rows"][9]["EDITED"] > 2
              and "The last edit date moved" in text,
              "the edit is checked by reading the last edit date back")
        check(text.startswith("replicakeep:") and "wrote" in out,
              "the report file holds the report")

        # One short target row that matches two datasets sends both edits to
        # one layer. Both would read APPLIED, so the run stops before any
        # request.
        stub = setup()
        put("tg2.csv", "dataset,layer_url\nPLANTS,%s\nROADS,%s\n"
            % ((stub.base + "/arcgis/rest/services/plants/FeatureServer/0",)
               * 2))
        code, out, err, text = go(stub)
        check(code == 2 and not stub.requests and "one layer" in err
              and "refused before any edit" in text,
              "two datasets resolved to one layer stop the run before any "
              "request  <-- pinned defect")
        stub = setup()
        code, out, err, text = go(stub, full=True)
        check(code == 1 and text.count("APPLIED") == 3
              and "3 more hold anchors" in text,
              "a repaired run is still exit 1 while replicas that cannot be "
              "judged remain  <-- pinned defect")

        for ending in ("\r\n", "\n", "  "):
            stub = setup()
            code, out, err, text = go(stub, env=secret + ending)
            check(code == 0 and all(r["token"] == "Bearer " + secret
                                    for r in stub.requests),
                  "a token with %r after it is trimmed, not rejected  "
                  "<-- pinned defect" % ending)
        for bad in ("tok QQ-LEAK2", "tok\x01QQ-LEAK2", "t\u00e9k-QQ-LEAK2"):
            code, out, err = run(argv + [
                "--targets", path("tg2.csv"), "--report", path("tok.txt"),
                "--apply"], env=bad)
            check(code == 2 and "QQ-LEAK2" not in out + err
                  and TOKEN_ENV in err and not os.path.exists(path("tok.txt")),
                  "a token with a space, a control or a non-ASCII character "
                  "is refused before the report is opened, and its text is "
                  "never echoed  <-- pinned defect")

        stub = setup(tracking=False)
        code, out, err, text = go(stub, env=None)
        check(code == 1 and "Editor tracking is off" in text
              and text.count("    UNVERIFIED ") == 3 and "APPLIED" not in text
              and "could not be checked" in text and len(writes(stub)) == 3
              and all(r["token"] is None for r in stub.requests),
              "a layer with no editor tracking is edited, says it cannot be "
              "checked, and leaves its replicas findings (exit 1, not 0)  "
              "<-- pinned defect")

        # Two edits that pick one row: the stamp of the first write must not
        # confirm the second. The stub stops stamping after one write.
        stub = setup()
        stub.layer("plants")["freeze_after"] = 1
        code, out, err, text = go(stub)
        check(code == 2 and text.count("    APPLIED ") == 2
              and text.count("    FAILED ") == 1 and "did not move" in text
              and len(writes(stub)) == 3,
              "a third edit of a row that two edits already stamped is "
              "checked against the stamp just before its own write  "
              "<-- pinned defect")
        stub = setup()
        code, out, err = run(argv + [
            "--targets", path("tg2.csv"), "--report", path("zero.txt"),
            "--apply", "--only", "109"], env=secret)
        stub.close()
        with open(path("zero.txt"), encoding="utf-8") as handle:
            zero = handle.read()
        check(code == 0 and not stub.requests
              and "No edit was planned, so nothing was written" in zero
              and "made and checked" not in zero,
              "a zero-edit --apply run does not claim every edit was made")

        stub = setup()
        stub.layer("plants")["tracking"] = False
        stub.layer("roads")["tracking"] = False
        code, out, err, text = go(stub)
        check(code == 2 and "did not move" in text and len(writes(stub)) == 3
              and text.count("    FAILED ") == 3,
              "a success the last edit date does not confirm is a failure, "
              "and the other edits are still tried  <-- pinned defect")

        stub = setup()
        stub.layer("plants")["tracking"] = "null"
        stub.layer("roads")["tracking"] = "null"
        code, out, err, text = go(stub)
        check(code == 2 and "did not move" in text
              and text.count("    FAILED ") == 3,
              "a last edit date that comes back empty is a failure  "
              "<-- pinned defect")

        stub = setup()
        stub.layer("roads")["raw"]["0"] = (
            200, b'{"error": {"code": 498, "message": "Invalid"}}', [])
        code, out, err, text = go(stub)
        check(code == 2 and "code 498" in text and not writes(stub),
              "the service's own error code is reported")

        for label, body in (
                ("success false", {"updateResults": [{"success": False}]}),
                ("an empty updateResults", {"updateResults": []}),
                ("two results", {"updateResults": [{"success": True}] * 2}),
                ("a result that is not an object", {"updateResults": [1]}),
                ("a service error", {"error": {"code": 500,
                                               "message": "boom"}})):
            stub = setup()
            stub.layer("roads")["apply"] = body
            code, out, err, text = go(stub)
            check(code == 2 and text.count("APPLIED") == 2
                  and "An edit FAILED" in text and len(writes(stub)) == 3,
                  "%s on one edit exits 2 and the report says which "
                  "edit failed" % label)

        def drop_editable(layer):
            layer["info"]["fields"] = layer["info"]["fields"][:3]

        def two_oids(layer):
            layer["info"]["fields"] = layer["info"]["fields"] * 2

        def lose_row(layer):
            layer["ids"]["1=1"] = [7]
            layer["rows"].pop(7)

        def lose_field(layer):
            layer["rows"][7].pop("LABEL")

        def lock_label(layer):
            for field in layer["info"]["fields"]:
                if field["name"] == "LABEL":
                    field["editable"] = False

        def raw(status, body, headers=()):
            return lambda layer: layer["raw"].update(
                {"0": (status, body, list(headers))})

        mutations = (
            ("no Update capability",
             lambda layer: layer["info"].update(capabilities="Query")),
            ("a branch versioned layer",
             lambda layer: layer["info"].update(isDataBranchVersioned=True)),
            ("no editable attribute field", drop_editable),
            ("an attribute field that is not editable", lock_label),
            ("two OID fields", two_oids),
            ("no row that satisfies the filter",
             lambda layer: layer["ids"].update({"1=1": []})),
            ("object ids that are not integers",
             lambda layer: layer["ids"].update({"1=1": ["a"]})),
            ("a row that does not come back", lose_row),
            ("a row without the field", lose_field),
            ("an HTTP 500", raw(500, b"x")),
            ("a body that is not JSON", raw(200, b"nope")),
            ("a body that is a JSON array", raw(200, b"[1]")),
            ("an error body with a code",
             raw(200, b'{"error": {"code": 498, "message": "Invalid"}}')),
            ("an error body that is not an object",
             raw(200, b'{"error": "x"}')),
        )
        for label, mutate in mutations:
            stub = setup()
            mutate(stub.layer("roads"))
            code, out, err, text = go(stub)
            check(code == 2 and not writes(stub)
                  and "refused before any edit" in text
                  and "replica 104" in text and secret not in out + err + text,
                  "%s stops the run before the first write  "
                  "<-- pinned defect" % label)

        stub = setup()
        stub.layer("roads")["raw"]["0"] = (302, b"", [
            ("Location", stub.base + "/evil")])
        code, out, err, text = go(stub)
        check(code == 2 and not any(r["path"] == "/evil"
                                    for r in stub.requests),
              "a redirect is refused, so the token is never resent to "
              "its target  <-- pinned defect")

        global MAX_RESPONSE_BYTES
        kept, MAX_RESPONSE_BYTES = MAX_RESPONSE_BYTES, 10
        try:
            stub = setup()
            code, out, err, text = go(stub)
        finally:
            MAX_RESPONSE_BYTES = kept
        check(code == 2 and "larger than" in err and not writes(stub),
              "a response over the size limit is refused")

        stub = setup()
        code, out, err, text = go(stub, ["--max-edits", "2"])
        check(code == 2 and not stub.requests and "--max-edits" in err,
              "a plan above --max-edits is refused before any request")

        stub = setup()
        put("tg2.csv", "dataset,layer_url\nOWNER.PLANTS,%s\n"
            % (stub.base + "/arcgis/rest/services/plants/FeatureServer/0"))
        code, out, err, text = go(stub)
        check(code == 2 and not stub.requests and "replica(s) 104" in err,
              "a dataset with no target stops the run before any request")

        stub = setup()
        for url, label in (
                (stub.base + "/arcgis/rest/services/p/MapServer/0",
                 "a MapServer URL"),
                ("http://example.org/x/FeatureServer/0",
                 "plain http to another host"),
                ("ftp://example.org/x/FeatureServer/0", "a non-http scheme"),
                ("https://h.example/x/FeatureServer/0?token=QQ-LEAK",
                 "a URL that holds a query string"),
                ("https://user:QQ-LEAK@h.example/x/FeatureServer/0",
                 "a URL that holds a login")):
            put("tg2.csv", "dataset,layer_url\nPLANTS,%s\nROADS,%s\n"
                % (url, url.replace("/0", "/1", 1)))
            OPENER.open = lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("network used"))
            try:
                code, out, err = run(apply_args(), env=secret)
            finally:
                OPENER.open = real_open
            check(code == 2 and "QQ-LEAK" not in out + err
                  and "AssertionError" not in err,
                  "%s is refused without a connection or an echo" % label)
        stub.close()

        put("tg2.csv", "dataset,layer_url\nPLANTS,http://127.0.0.1:1/x/"
            "FeatureServer/0\nROADS,http://127.0.0.1:1/y/FeatureServer/0\n")
        code, out, err = run(apply_args(), env=secret)
        check(code == 2 and "request failed" in err
              and secret not in out + err,
              "a connection that fails exits 2 and never echoes the token")

        stub = setup()
        code, out, err = run(argv + ["--targets", path("tg2.csv"), "--report",
                                     tmp, "--apply"], env=secret)
        stub.close()
        check(code == 2 and not stub.requests and "could not write" in err,
              "a report that cannot be written stops the run before the "
              "first edit  <-- pinned defect")

        put("tg3.csv", "dataset,layer_url\nPLANTS,https://h.example/x/"
            "FeatureServer/0?token=QQ-LEAK\n")
        code, out, err = run(argv + ["--targets", path("tg3.csv")])
        check(code == 1 and "QQ-LEAK" not in out + err
              and "not shown, the URL holds" in out,
              "a plan-only run never prints a target URL that holds a "
              "query string or login  <-- pinned defect")

        put("tg2.csv", "dataset,layer_url\nPLANTS,https://127.0.0.1:1/x/"
            "FeatureServer/0\nROADS,https://127.0.0.1:1/y/FeatureServer/0\n")
        code, out, err = run(apply_args(), env=secret)
        check(code == 2 and "request failed" in err,
              "an https target is accepted and reaches the network layer")

        unanchored = fixture()
        unanchored["versions"] = [r for r in unanchored["versions"]
                                  if r["name"] != "SYNC_SEND_77_1"]
        for r in unanchored["versions"]:
            r["row_count"] = len(unanchored["versions"])
        check("ANCHOR WITHOUT" not in "\n".join(
            describe(analyse(build(**unanchored)), 6)),
            "a database with no anchor lacking a replica says nothing "
            "about one")

        real_analyse = globals()["analyse"]
        for boom, name in ((lambda *a, **k: 1 / 0, "ZeroDivisionError"),
                           (lambda *a, **k: {}["k"], "KeyError"),
                           (lambda *a, **k: math.exp(1000), "OverflowError")):
            globals()["analyse"] = boom
            try:
                code, out, err = run(argv)
            finally:
                globals()["analyse"] = real_analyse
            check(code == 2 and name in err and "not a finding" in err,
                  "an unexpected %s exits 2, never the 1 Python would "
                  "give  <-- pinned defect" % name)

        meta = Harness()
        with contextlib.redirect_stdout(io.StringIO()) as sink:
            meta.check(False, "m1")
            meta.raises(lambda: None, "m2")
            meta.raises(lambda: 1 / 0, "m3")
            meta.raises(lambda: 1 / 0, "m4", ZeroDivisionError)
            code = finish(meta.total(), meta.failed)
        check(len(meta.failed) == 3 and meta.failed[0] == "m1"
              and meta.passed[0] == 1 and code == 1
              and "3 failed" in sink.getvalue(),
              "the harness records a failed check, a missing error and a "
              "wrong error, and the footer exits 1")

        # ---- the real command line, as a separate process
        stub = setup()
        env = dict(os.environ)
        env[TOKEN_ENV] = secret
        script = os.path.abspath(__file__)
        done = subprocess.run(
            [sys.executable, script] + apply_args(), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stub.close()
        check(done.returncode == 0 and len(writes(stub)) == 3
              and secret.encode() not in done.stdout + done.stderr
              and all(r["token"] == "Bearer " + secret
                      for r in stub.requests),
              "the real command line reads the token from the environment, "
              "edits, and prints no token")
        listing = sorted(os.listdir(tmp))
        done = subprocess.run([sys.executable, script] + argv + ["--ap"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        check(done.returncode == 2 and done.stdout == b""
              and sorted(os.listdir(tmp)) == listing,
              "the real command line refuses --ap and writes nothing")
        done = subprocess.run([sys.executable, script] + argv,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        check(done.returncode == 1 and b"VERDICT" in done.stdout,
              "the real command line exits 1 on a finding")
        done = subprocess.run([sys.executable, script, "--versions", "x"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        check(done.returncode == 2, "the real command line exits 2, not 1, "
              "when it cannot do its job")
    return finish(harness.total(), failed)


def finish(total, failed):
    """The self-test footer, and its exit code."""
    if failed:
        print("%d assertions, %d failed" % (total, len(failed)))
        for f in failed:
            print("  FAILED: %s" % f)
        return 1
    print("%d assertions, 0 failed" % total)
    return 0


# Coverage runs this file as __main__, so the false arm of this test is never
# taken in a measured run. The pragma names that one arm and nothing else.
if __name__ == "__main__":  # pragma: no branch
    # A redirected stdout on Windows is cp1252, and a replica name can hold
    # any character. Escape it rather than crash mid-report.
    sys.stdout.reconfigure(errors="backslashreplace")
    sys.exit(main())
