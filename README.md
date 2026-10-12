# replicakeep

Plan, then repair under `--apply`, an idle replica that may hold an enterprise geodatabase's compress floor.

An enterprise geodatabase ran its compress every night. Every night the job exited 0 and the
log said it succeeded. For months the geodatabase never got close to state 0. A layer of
facility points was shared to a web portal through a distributed collaboration, and nobody had
edited it since the spring. Nothing was broken. The layer was idle.

Its replica kept a hidden `SYNC_SEND` system version at the state of the last generation it
had acknowledged. Compress cannot fold the states that a version still points at, so it
stopped at that state each night and still exited 0. The system versions of a replica are the
anchors in this README.

In the logs this tool was built from, an acknowledged generation moved only after a sync that
carried an edit inside the replica's scope. A layer with no such edit kept the same generation,
and its anchors stayed where they were. The repair that was seen to work is small: rewrite one
attribute of one row with the value it already has. That is a real edit, and the tool sends the
value back as it read it. The next sync carries the edit, and the generation moves.

This is an observation from one estate, not a documented rule. Esri's pages say that you must
synchronize regularly for compress to be effective. They do not describe this case. The logs
that showed it are not published with the tool. Treat it as something that was seen, and check
it on your own estate: the proof is a later export in which the generation moved.

replicakeep reads three exported tables, finds the idle replicas, and plans the smallest edit
that could move each one. Without `--apply` it only prints the plan. With `--apply` it makes
the edits through the REST API of a feature service that you name.

A clean exit from this tool means it looked and found nothing. It never means it could not
tell. A replica log with no usable rows, a replica that holds anchors and does not sync, and a
replica with no known generation are all exit 1 or exit 2, not exit 0.

```
replicakeep self-test: no database, no real service, no credentials
--------------------------------------------------------------------
PASS  the as-of time is the export's database clock, not the local clock
PASS  a replica idle 100 days that still syncs is IDLE
PASS  a SYNC_RECEIVE_REC_ version is an anchor of its replica
...
PASS  idle time runs from the LAST change of generation, not the first row  <-- pinned defect
...
PASS  --only with an id no replica item carries is refused, never an all-clear for nothing  <-- pinned defect
...
PASS  an empty replica log is refused, never read as no replica syncing  <-- pinned defect
...
PASS  a replica whose recent syncs all fail is NOT_SYNCING, not IDLE, and gets no edit  <-- pinned defect
...
PASS  a NOT_SYNCING replica is a finding by default, never a clean exit  <-- pinned defect
...
PASS  the abbreviation --ap is refused by the parser  <-- pinned defect
...
PASS  a repaired run is still exit 1 while replicas that cannot be judged remain  <-- pinned defect
PASS  a token with '\r\n' after it is trimmed, not rejected  <-- pinned defect
...
PASS  a token with a space, a control or a non-ASCII character is refused before the report is opened, and its text is never echoed  <-- pinned defect
...
PASS  an attribute field that is not editable stops the run before the first write  <-- pinned defect
...
PASS  a redirect is refused, so the token is never resent to its target  <-- pinned defect
...
PASS  a plan-only run never prints a target URL that holds a query string or login  <-- pinned defect
...
209 assertions, 0 failed
```

The block above is a real run, cut to 15 of its 209 PASS lines. Each line is verbatim, and
`...` marks the lines left out.

## What already exists

- **Esri, "Replication and versioning".** It says that replica system versions exist and that
  "you need to synchronize regularly to achieve an effective compress". That is the
  authoritative statement of the problem. It does not say what to do for a replica whose layer
  nobody edits.
- **A person who edits one row by hand.** This is the repair that was seen to work, and it needs
  no tool. It does not scale past a few replicas. It also needs someone to remember it, to pick a
  row that satisfies the replica's own filter, and to know which replicas share a dataset.
- **[compressfloor](https://github.com/uhsear/compressfloor).** It reads exported system tables
  and names what holds the floor. It diagnoses, and it prints an unregister plan. It does not
  repair an idle replica. replicakeep is the repair for the one holder that a single edit can
  release.
- **[geodatabase-replication-toiler](https://github.com/mattyschell/geodatabase-replication-toiler).**
  Scripts to create, check and test replicas, and a runbook that reads `GDB_REPLICALOG` to find
  why a replica failed. It does well at the life of a replica. Its README and its runbook say
  nothing about compress or an idle layer.

The search was `gh search repos` on 2026-10-12, run again after this repository was created.
"replica keepalive arcgis" and "compress floor geodatabase" returned nothing. "replicakeep"
returned only this repository. "arcgis replica" returned an Android sample and
`rahmatya685/arcgis-replica`, a 2020 repository whose one file is an arcpy script that builds a
check-out replica from a query. "geodatabase replica" returned the toiler above,
`gis-jay/SqlServerConnector` (a Python 2.7 loader between a plant records database and an
enterprise geodatabase, whose description names a "single geodatabase replica design") and `tom-montgomery/Replica2MXD`
(builds map documents from replicas). I opened `rahmatya685/arcgis-replica`, the SQL Server
connector and the toiler, and read the README or the script of each. I did not open the Android
sample. I read only the description of Replica2MXD and the first lines of its README. None of
the ones I opened does what this tool does. A code search for `GDB_REPLICALOG` returned data
models, GDAL source and tests, a table script, notebooks of a file geodatabase reader, and the
toiler runbook. That says nothing about private code, and it is not a claim that no such tool
exists.

## Requirements

Python 3.9 or newer and nothing else. No `arcpy`, no database driver, no third-party package.
The tool reads files, and under `--apply` it calls a feature service over HTTP.

The same 209 assertions pass on Windows (Python 3.13.2), on Windows (Python 3.9.25) and on
Ubuntu 24.04 (Python 3.12.3). The labels of the PASS lines are identical on all three. The
self-test takes about 6 seconds on Windows. Branch coverage of `replicakeep.py` is 100 percent with one
pragma. See "Coverage".

```
git clone https://github.com/uhsear/replicakeep.git
python replicakeep.py --self-test
```

## Usage

1. Export three tables from the geodatabase, as CSV or as a JSON array of objects. "The
   exports" below gives the columns and a tested PostgreSQL query for each.
2. Run the plan. Nothing is written and no network is used.
3. To repair, write a targets file and run again with `--apply`.

```
python replicakeep.py --versions versions.csv --replicas replicas.csv --replica-log replica_log.csv
python replicakeep.py ... --min-idle-days 14 --silent-days 3 --only 101,103
python replicakeep.py ... --targets targets.csv --report plan.txt --apply
```

The token for `--apply` comes from the `REPLICAKEEP_TOKEN` environment variable. There is no
flag for it, so it cannot land in a shell history or a process list. The tool sends it in the
`X-Esri-Authorization` header and never puts it in a URL. It trims a space, a newline or a
carriage return from either end of the value, because a copied line or a CRLF file leaves one
there. It refuses a token that still holds a space, a control character or a character outside
ASCII, and it does not print the value.

| Flag | Default | What it does |
|---|---|---|
| `--versions` | none | The version table export. Required. |
| `--replicas` | none | The replica items export, with the Definition XML. Required. |
| `--replica-log` | none | The replica log export. Required. |
| `--targets` | off | Dataset name to feature layer URL, as CSV or JSON. `--apply` needs it. Without `--apply`, it only shows which target each edit would use. |
| `--min-idle-days` | `6` | Plan an edit for a replica whose acknowledged generation has not moved for at least this many days. |
| `--silent-days` | `3` | A replica with no successful logged event for more than this many days is not syncing. No edit can help it. |
| `--event` | `1` | The log event to read the generation from. Esri defines 1 as an import and 2 as an export. |
| `--only` | all | Replica ids, comma separated. All other replicas are left out of the plan. There is no `all`. An id that no replica item carries exits 2. |
| `--max-edits` | `10` | Refuse an `--apply` run that plans more edits than this. |
| `--as-of` | the export time | Measure ages from this time instead of the `exported_at` time in the versions export. A time earlier than the newest log row exits 2. |
| `--exit-on` | `idle` | What makes the exit code 1: `idle`, `any` or `never`. See "Exit codes". |
| `--report` | off | Write the report to this file. Needs `--apply`. The file is overwritten. |
| `--apply` | off | Make the edits, and write `--report`. Needs `--targets` and `--report`. |
| `--self-test` | off | Run the offline assertions and exit. Takes no other flag. |

The parser refuses an abbreviated flag, so `--ap` is not `--apply`. The self-test pins this
for seven prefixes: `--ap`, `--repor`, `--replica-l`, `--exit`, `--vers`, `--min-idle` and
`--sil`. Each is given as a complete, valid argument list, so only the parser can refuse it.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | No finding at the `--exit-on` level. After a successful `--apply`, the replicas that were edited are not findings. |
| 1 | At least one finding. By default (`--exit-on idle`) a finding is an idle replica that the run did not repair, or a replica that holds anchors and that the tool could not judge or no edit can move: `NOT_SYNCING`, `NO_ACK` or `NO_DATASET`. `--exit-on any` also counts an anchor with no replica item. |
| 2 | The tool could not do its job: an export could not be read, an export is empty or disagrees with itself, a flag or the token was wrong, a URL was refused, a read failed, or an edit was refused or could not be checked. Three more cases are exit 2: a replica log with no row for any replica that holds anchors, anchors with no usable replica item, and an `--only` id that no replica item carries. |

Exit 2 is kept apart from exit 1 on purpose. A scheduler that cannot tell "the tool failed"
from "findings exist" will show a failure every day, or will hide one. These are the same codes
that [compressfloor](https://github.com/uhsear/compressfloor) uses, so one monitor can read both.
`--exit-on never` keeps exit 2 and turns every finding into exit 0.

The three statuses `NOT_SYNCING`, `NO_ACK` and `NO_DATASET` are findings by default. An earlier
version left them at exit 0. In each of them a replica holds anchors, and the tool either cannot
move them or cannot tell. An exit 0 there would be the same false green that the opening
describes. If a replica syncs less often than `--silent-days`, raise that limit. Then the tool
reads its last good sync, and plans an edit if it is idle.

### The exports

Take the exports after the compress has finished. Each CSV needs a `row_count` column, and the
versions export needs `exported_at`. The tool compares `row_count` with the rows it read. A
paged copy or a row limit that lost rows would otherwise move a replica under the limit and
turn a finding into a clean exit 0. The same rule is in compressfloor. The versions export
takes its clock from the database, so the same files give the same plan on any day.

A header-only file cannot show that rows were lost, so it is not checked by `row_count`. The
tool refuses instead when the replica log holds no row for any server replica that holds
anchors. That covers an empty or truncated log, and a log that joins to no replica item.

| Export | Columns | Notes |
|---|---|---|
| `versions` | `name`, `exported_at`, `row_count` | The export that compressfloor `--sql` prints works as it is. Other columns are ignored. |
| `replicas` | `objectid`, `item_type`, `definition`, `row_count` | `definition` is the item's XML text. A `Sync Replica` item and an item with no usable ID are counted and left out. |
| `replica_log` | `replicaid`, `logdate`, `event`, `errorcode`, `targetgen`, `row_count` | Esri says `replicaid` corresponds to `objectid` in `GDB_ITEMS`, not to the ID inside the XML. The tool joins on `objectid`. Export the whole table. |
| `targets` | `dataset`, `layer_url` | Written by hand. A dataset may be named in full or by its trailing parts, so `OWNER.TABLE` and `TABLE` both match `DB.OWNER.TABLE`. A dataset that matches two rows matches none. |

These PostgreSQL queries were run on 2026-10-12 with `psql --csv` against PostgreSQL 16.15
(the `postgres:16-alpine` image, in a throwaway container that was removed afterward), over a
synthetic geodatabase schema filled with this tool's fixture. The tool read the three files and
printed the same verdicts as it did for the fixture's own CSV files: a diff of the two plans,
without the as-of line, was empty. Add your own connection options.

```sql
-- versions.csv
SELECT name, owner, state_id,
       to_char(LOCALTIMESTAMP, 'YYYY-MM-DD HH24:MI:SS') AS exported_at,
       COUNT(*) OVER () AS row_count
FROM sde.sde_versions;

-- replicas.csv
SELECT items.objectid AS objectid, itemtypes.name AS item_type,
       items.definition::text AS definition,
       COUNT(*) OVER () AS row_count
FROM sde.gdb_items AS items
INNER JOIN sde.gdb_itemtypes AS itemtypes ON items.type = itemtypes.uuid
WHERE itemtypes.name IN ('Replica', 'Sync Replica');

-- replica_log.csv
SELECT replicaid,
       to_char(logdate, 'YYYY-MM-DD HH24:MI:SS') AS logdate,
       event, errorcode, targetgen, COUNT(*) OVER () AS row_count
FROM sde.gdb_replicalog;
```

The SQL Server form of the same three SELECTs uses `CONVERT(varchar(19), ..., 120)` for dates,
`CAST(items.Definition AS nvarchar(max))` for the XML and the `sde.` table names from Esri's
system table pages. It was not run for this README, because no SQL Server was available. Do
not save the replicas export with `sqlcmd -s ","`. That client does not quote a field, and a
filter such as `KIND IN ('A','B')` holds a comma. Use a client that quotes, or export JSON.

## What it decides

For every server-type replica that holds anchors (`SYNC_SEND_`, `SYNC_RECEIVE_` or
`SYNC_RECEIVE_REC_` followed by the replica ID and a generation, as TA 000011719 names them),
it takes one status. A syncing replica is one with a successful logged event of any kind
(an `errorcode` equal to the success code) in the last `--silent-days`. A replica whose recent
events are all failures does not sync.

| Status | When | What happens |
|---|---|---|
| `IDLE` | The replica syncs, and its acknowledged generation has not changed for `--min-idle-days` or more. | An edit is planned. |
| `COVERED` | Same as `IDLE`, and an earlier replica's edit uses the same dataset and the same filter. | No second edit. One edit covers both. |
| `ACTIVE` | The generation moved less than `--min-idle-days` ago. | Nothing. |
| `NOT_SYNCING` | No successful event for more than `--silent-days`, or no log row at all. | Nothing. An edit cannot help a replica that does not sync. The report points to `compressfloor --unregister-plan`. Exit 1 by default. |
| `NO_ACK` | It syncs, but the log has no successful row of the chosen event. | Nothing. The generation is unknown. Exit 1 by default. |
| `NO_DATASET` | Idle, but the definition lists no feature class or table. | Nothing. Exit 1 by default. |
| `CLIENT` | Not a server-type replica, such as a Field Maps offline replica. | Never edited. |

A replica that holds no anchor pins nothing and is left alone. An anchor whose replica ID has no
`Replica` item is reported as an anchor without a replica item. Run compressfloor on those. It
names detached versions, and replicakeep cannot help them.

The idle time runs from the date of the last change of generation among the successful rows of
the chosen event, to the database clock. The first row is a lower bound, not a start. A replica
whose generation changed 40 days ago, after 90 quiet days, is idle for 40 days. The self-test
pins this.

A `COVERED` replica depends on the observation that replicas sharing a dataset share one edit.
The tool does not share an edit across two filters. The edit must satisfy each replica's own
filter, and a row that satisfies one may not satisfy the other.

## What an edit is

The plan lists, for each edit, the dataset and the filter from the replica's own definition. At
`--apply` time the tool does the following, in two phases.

Phase one reads and writes nothing. For every planned edit it reads the layer description,
picks the first editable attribute field that is not a tracking field, asks for the object IDs
that satisfy the filter, takes the lowest, and reads that row without geometry. If any of these
reads fails, the run stops before the first write and exits 2.

Phase two writes. For each edit it sends one `applyEdits` request with one update and
`rollbackOnFailure` set to `true`. The update holds the object ID and the one field, with the
value the row already had. It sends no geometry. Then it reads the row back. If the layer has
editor tracking, the last edit date must have changed, or the edit is reported as failed.

The edit is a real edit. If editor tracking is on, it restamps the editor and the edit date of
that one row. That is the cost of the repair, and the plan says so before any write.

The tool never deletes a version, never unregisters a replica and never opens a database. It
edits only a URL that ends in `/FeatureServer/<number>` and that is https, or http to the local
machine. A URL that holds a query string, a fragment or a login is refused without being
echoed, and a plan-only run shows such a target as hidden. A redirect is refused, because
`urllib` would resend the token header to the target. A layer without `Update` in its
capabilities is refused. A branch versioned layer is refused, because the tool edits the
version the service is published against.

## Worked example

This is the fixture from the self-test, written to CSV files. The replica names are synthetic.
Replica 102 shares the dataset and filter of replica 101. Replica 103 uses the same dataset with
another filter.

```
$ python replicakeep.py --versions versions.csv --replicas replicas.csv --replica-log replica_log.csv
replicakeep: idle replicas that may hold the compress floor
as of 2026-03-31 12:00:00, the database clock of the versions export
This is an observation. An edit is the repair that was seen to work; the proof is a later export showing the generation moved.

IDLE        replica 101 'alpha_to_web': generation 12 unchanged for 100 day(s); holds 3 anchor version(s)
COVERED     replica 102 'bravo_to_web': generation 12 unchanged for 100 day(s); holds 2 anchor version(s)
IDLE        replica 103 'charlie_to_web': generation 12 unchanged for 100 day(s); holds 2 anchor version(s)
IDLE        replica 104 'delta_to_web': generation 6 unchanged for 40 day(s); holds 2 anchor version(s)
CLIENT      replica 105 'echo_offline': not a server-type replica, so it is never edited
NOT_SYNCING replica 106 'foxtrot_to_web': holds 2 anchor version(s) and logged no successful event in the last 3 day(s). An edit cannot help a replica that does not sync. If it syncs less often, raise --silent-days. If it is abandoned, compressfloor --unregister-plan orders its removal.
NO_ACK      replica 107 'golf_to_web': holds 2 anchor version(s) and syncs, but the log has no successful event 1 row, so the acknowledged generation is unknown
NO_DATASET  replica 108 'hotel_to_web': generation 4 unchanged for 100 day(s), but the definition lists no feature class or table to edit
ACTIVE      replica 109 'india_to_web': generation 4 moved 2 day(s) ago, under the 6-day limit
1 replica item(s) hold no anchor or were left out by --only; 2 item(s) were not a usable GPReplica.
ANCHOR WITHOUT A REPLICA ITEM: replica id(s) 77. No edit helps these. Run compressfloor, which names them.

PLAN
  edit 1 of 3 for replica 101 (covers 101, 102): dataset SYNTHDB.OWNER.PLANTS, filter (KIND='A')
    rewrite one attribute of the lowest OBJECTID that satisfies the filter with its own value.
    target: none
  edit 2 of 3 for replica 103 (covers 103): dataset SYNTHDB.OWNER.PLANTS, filter (KIND='B')
    rewrite one attribute of the lowest OBJECTID that satisfies the filter with its own value.
    target: none
  edit 3 of 3 for replica 104 (covers 104): dataset SYNTHDB.OWNER.ROADS, filter none
    rewrite one attribute of the lowest OBJECTID that satisfies the filter with its own value.
    target: none

VERDICT: 4 idle replica(s) at 6 day(s) or more, 3 edit(s) planned; 3 more hold anchors that an edit cannot move or that could not be judged. Nothing was written.

Check only. Nothing was written and no network was used.
$ echo $?
1
```

The same files with `--targets targets.csv --report plan.txt --apply`, against a local stub of
a feature service (see "Where each claim was exercised"). The report is cut after the plan
heading. The port is the stub's:

```
PLAN
  edit 1 of 3 for replica 101 (covers 101, 102): dataset SYNTHDB.OWNER.PLANTS, filter (KIND='A')
    rewrite one attribute of the lowest OBJECTID that satisfies the filter with its own value.
    target: http://127.0.0.1:56131/arcgis/rest/services/plants/FeatureServer/0
    APPLIED OBJECTID 9, field LABEL rewritten with its own value. The last edit date moved.
  edit 2 of 3 for replica 103 (covers 103): dataset SYNTHDB.OWNER.PLANTS, filter (KIND='B')
    rewrite one attribute of the lowest OBJECTID that satisfies the filter with its own value.
    target: http://127.0.0.1:56131/arcgis/rest/services/plants/FeatureServer/0
    APPLIED OBJECTID 7, field LABEL rewritten with its own value. The last edit date moved.
  edit 3 of 3 for replica 104 (covers 104): dataset SYNTHDB.OWNER.ROADS, filter none
    rewrite one attribute of the lowest OBJECTID that satisfies the filter with its own value.
    target: http://127.0.0.1:56131/arcgis/rest/services/roads/FeatureServer/0
    APPLIED OBJECTID 7, field LABEL rewritten with its own value. The last edit date moved.

VERDICT: 4 idle replica(s) at 6 day(s) or more, 3 edit(s) planned; 3 more hold anchors that an edit cannot move or that could not be judged. Every edit was made as listed.

wrote plan.txt
exit 1
```

The exit code is 1 on purpose. The three idle replicas were edited and are no longer findings,
but replicas 106, 107 and 108 still hold anchors that this run cannot move or judge. The run
sent nine read requests before its first write. After the three edits, the stub's rows held
their old values and a newer edit date. Nothing in the output, the report or any URL held the
token. The next step is outside the tool: wait for the next sync, export again, and run the
plan again. A replica whose generation moved is `ACTIVE` in that second plan.

## Where each claim was exercised

| Claim | Where it was exercised |
|---|---|
| A plan-only run writes no file and opens no connection | Self-test: the directory listing is unchanged, and `OPENER.open` is replaced by a function that fails if it is called. Also a real command line run. |
| `--apply` edits through REST and verifies | Self-test, against a local `http.server` stub that records every request. Also one real command line run as a separate process, with the token in its environment. |
| The token is only in the header | Self-test: every recorded request carries the header, and the token is in no recorded path, query or body, in no output and not in the report file. The example above was run as a separate process, and its saved output, report and targets file were checked for the token: no match. |
| A token with a stray newline works, and a bad token is never echoed | Self-test: a token followed by `\r\n`, `\n` or two spaces is trimmed and used. A token with a space, a control character or a non-ASCII character exits 2 before the report file is opened, and its text is in no output. |
| No write happens if any read fails | Self-test: fourteen layer and row faults on the last edit, each with zero `applyEdits` requests. |
| A failed edit exits 2 and names itself | Self-test: five bad update responses, a success the edit date does not confirm, and an edit date that comes back empty. |
| A redirect is not followed | Self-test: the stub answers 302, and no request reaches the target. |
| A bad URL is refused without a connection | Self-test: a MapServer URL, plain http to another host, another scheme, a query string and a login. |
| Abbreviated flags are refused | Self-test, seven complete argument lists, and one real command line run of `--ap`. |
| An unexpected error exits 2, not 1 | Self-test: `analyse` is replaced in turn by functions that raise `ZeroDivisionError`, `KeyError` and `OverflowError`. |
| An empty log, an unjoined log, no readable replica item and an unknown `--only` id exit 2 | Self-test, each by a call and a header-only log by the command line. |
| A replica that cannot be judged is exit 1 | Self-test: each status through `gate`, and two runs of `main()`, one of them after a successful `--apply`. |
| The JSON form of a targets file works | Self-test: a plan-only run with a JSON targets file. |
| The PostgreSQL queries produce the exports | A run on 2026-10-12 against PostgreSQL 16.15 with `psql --csv`, compared with the fixture's own plan. |
| A DTD in a definition is refused | Self-test. |

I also ran 100 one-line mutations of the decision and write paths with a script. All 100 were killed by the self-test. None survived. An earlier sweep of 86 mutations against the first version of this tool left 29 survivors, and each of those now has an assertion that kills it. One mutation of that sweep was dropped, because it cannot change behavior: `gate` returns first for `never`, and the parser allows no fourth value.
The script is not in the repository. Some of the mutations undo this version's fixes one at a
time: the token trim and check, the empty and unjoined log checks, the unusable replica check,
the unknown `--only` check, the success filter of the silence check, and each status that makes
exit 1. The self-test kills every one of them.

Every limit below that describes how the tool behaves for a given input was reproduced once on
2026-10-12 by a throwaway script, against a local stub or a raw socket. Those runs are not in
the self-test.

## Coverage

```
python -m coverage run --branch replicakeep.py --self-test
python -m coverage report -m
replicakeep.py 1022 0 266 0 100%
```

The file ends with `if __name__ == "__main__":  # pragma: no branch`. Coverage runs the file
as `__main__`, so the false arm of that test is never taken in a measured run. Without the
pragma the report shows 99 percent and one partial branch, the exit arc of that test, and that
arm is the only difference. No other line is excluded.

## Limits

- **The core claim is an observation.** The logs are not published. Esri documents the need to
  synchronize regularly, not the case of an idle layer.
- **XML element names are observed, not documented.** Esri's Technical Article 000011719
  documents `/GPReplica/ID` and `/GPReplica/Name`. The tool also reads `TargetType`,
  `GPReplicaDataset`, `DatasetName`, `DatasetType` and `DefQuery`, as seen in the definitions
  this was built from. A different version could name them differently, and the tool would
  then find no dataset and plan no edit.
- **A definition whose root declares a default namespace is not read.** Its replica is counted
  as unread, and its anchors are reported as an anchor without a replica item. If no
  definition at all can be read while anchors exist, the run exits 2.
- **Error code 0 is assumed to mean success.** Esri says only that a successful event gets a
  success code. The constant `SUCCESS_CODE` is at the top of the file.
- **Which log event carries the acknowledgement is assumed to be event 1.** Esri defines 1 as an
  import and 2 as an export. `--event 2` reads the other.
- **The tool reads `targetgen` as the generation the replica acknowledged.** Esri documents
  `targetgen` only as the generation to which changes are to be applied. The reading is
  observed in the logs this was built from, not documented.
- **An update without geometry is assumed to leave the geometry alone.** Esri's `applyEdits`
  page does not say so, and its feature object page says only that all properties are
  optional. The tool sends no geometry because a round trip can flatten true curves. Test it on
  a copy of the layer first. The tool does not compare geometry before and after.
- **The value is sent back as the query returned it.** The tool does not check that every
  attribute type survives the JSON round trip exactly. A date with more precision than a
  millisecond, or a number with more digits than JSON keeps, could change.
- **The tool does not know that an edit moved the replica.** It checks that the service accepted
  the edit and, with editor tracking, that the row's edit date moved. Whether the replica's
  generation moves is visible only in a later export.
- **Editor tracking.** Esri's pages do not say that every edit restamps the tracking fields. The
  tool reads the edit date back and reports a layer where it did not move as a failure. On a
  layer without editor tracking the edit cannot be checked. It is reported as `APPLIED`, the
  run can exit 0, and the last line says "Every edit was made as listed." Only a later export
  shows whether the generation moved.
- **A layer with an attribute rule or a database trigger on update may change other data** when
  the one field is rewritten. The tool cannot see this.
- **Branch versioned layers are refused**, and the tool edits only the version a service is
  published against. A feature service that is not the data in the replica's scope would edit
  the wrong place. The targets file is your decision.
- **The lowest OBJECTID comes from a list of object IDs.** Esri limits that list to one million
  IDs. On a larger layer the row is the lowest one returned, which still satisfies the filter.
- **A `where` clause is the replica's filter, sent as written.** It must use field names that
  the service exposes. A filter that the service cannot parse fails in phase one. The filter
  text is not escaped for display, so a newline in a filter is printed as it is, and it can
  forge a `VERDICT` line in the plan and the report. Names are escaped. Filters are not.
- **A multi-dataset replica gets one edit, on its first dataset only.** If that dataset has no
  `--targets` row, the run exits 2, even when a later dataset of the replica has one.
- **A dataset name in a definition must match a target by whole name parts.** If the definition
  says `PLANTS` and the targets file says `OWNER.PLANTS`, they do not match, and `--apply`
  exits 2 with "no unique --targets row".
- **Give target URLs without a trailing slash.** `.../FeatureServer/0/` passes the layer check
  and builds `.../FeatureServer/0//query`. A real server may reject that, and the run then
  exits 2 in phase one, before any write.
- **The targets file decides where the token is sent.** There is no allowlist of hosts. Keep
  the file under the same control as the token.
- **Keep tokens out of the path of a target URL.** Only a query string, a fragment or a login
  is detected. A token in the path is printed in the plan.
- **The header is `X-Esri-Authorization`.** Esri documents it, and says header tokens are
  supported from ArcGIS Server 10.5.1. The tool sends no other header. A service that accepts
  only the standard `Authorization` header will refuse every request.
- **The tool uses the proxy settings of the environment.** With `HTTP_PROXY` set, a request to
  `http://127.0.0.1` goes to the proxy, and the `--self-test` runs of `--apply` fail. Set
  `no_proxy` for `127.0.0.1`, or unset `HTTP_PROXY`, before the self-test or a local target.
- **A connection that ends in the middle of a response is not caught.**
  `http.client.IncompleteRead` and `BadStatusLine` escape the request function. The run exits 2
  with a bare exception name. Under `--apply`, edits that were already sent are not written to
  the `--report` file, which is left empty. A raw socket that answers `GARBAGE NOT HTTP` showed
  it.
- **`--report` overwrites its file.** It is opened for writing before the first edit, so a file
  of that name is truncated, even when the run then refuses. Do not point it at an export or
  the targets file. The tool does not check.
- **A zero-edit `--apply` run says "Every edit was made as listed."** The line is vacuous. No
  edit was planned.
- **Export the whole replica log.** The idle time runs from the first log row when the
  generation never changes. A log cut to recent rows under-reports the idle time. The detail
  line then says "generation N moved D day(s) ago", with D the age of the first row kept, and an
  idle replica reads `ACTIVE` and exits 0. A log cut to one recent row showed it.
- **A blank cell in a log row stops the run.** A blank `errorcode`, `event`, `logdate` or
  `targetgen` in any row exits 2 and names the row. Filter or coalesce NULLs on failed events in
  the export.
- **A replica definition over 131072 characters exits 2** with "field larger than field limit
  (131072)". The message does not name the export. A definition with white space before its XML
  declaration exits 2 as "not XML". Strip the cell first.
- **An integer cell is read loosely.** `1_000` and non-ASCII digits are read as whole numbers,
  which is looser than the digit-only cells that the exports hold.
- **The tool has not been run against a real geodatabase or a real feature service.** Every
  network path was run against a local stub, and the PostgreSQL queries against a synthetic
  schema. The SQL Server queries were not run.
- **A replica list that is empty is exit 2.** A zero-byte file is what a failed export leaves,
  and read as no replicas it would call every anchor detached.
- **State locks are not visible.** A connected session's lock on an old state can hold compress
  too, and no export shows it.
- **The XML parser is the standard library's.** A definition with a DTD or an entity is refused
  before it reaches the parser.

## Sources

- [Replication and versioning](https://doc.esri.com/en/arcgis-pro/latest/help/data/geodatabases/overview/replication-and-versioning.html):
  "Because of these system versions, you need to synchronize regularly to achieve an effective
  compress." "Several replicas can also share the same replica version."
- Esri Technical Article 000011719, "How To: Determine if there are detached replica system
  versions in the geodatabase". The address
  https://support.esri.com/en/technical-article/000011719 now leads to an archive page. The
  copy read was the Wayback Machine copy of 5 August 2021. It gives the names
  `SYNC_RECEIVE_<replica id>_<generation number>`, `SYNC_RECEIVE_REC_...` and `SYNC_SEND_...`,
  the paths `/GPReplica/ID` and `/GPReplica/Name`, and the warning that no other replica
  system version should be deleted.
- [System tables of a geodatabase in SQL Server (10.1)](https://resources.arcgis.com/en/help/main/10.1/002q/002q00000080000000.htm):
  `GDB_REPLICALOG` columns, including `replicaid` ("Corresponds to the objectid field in the
  GDB_ITEMS table"), `event` (import 1, export 2), `errorcode`, `logdate` and `targetgen`.
- [Apply Edits (Feature Service/Layer)](https://developers.arcgis.com/rest/services-reference/enterprise/apply-edits-feature-service-layer/):
  the method is POST, `updates` carry the object ID in `attributes`, `rollbackOnFailure`
  defaults to `true`, each result has `success` and, on failure, an `error`, and without
  `gdbVersion` "edits are made to the published map's version".
- [Query (Feature Service/Layer)](https://developers.arcgis.com/rest/services-reference/enterprise/query-feature-service-layer/):
  `where`, `objectIds`, `outFields`, `returnGeometry`, `returnIdsOnly`, and the one million limit
  on the object ID array.
- [Layer (Feature Service)](https://developers.arcgis.com/rest/services-reference/enterprise/layer-feature-service/):
  `capabilities`, `fields` with `type`, `editable` and `nullable`, `editFieldsInfo`,
  `isDataVersioned` and `isDataBranchVersioned`.
- [HTTP authorization headers](https://developers.arcgis.com/documentation/security-and-authentication/reference/http-authorization-headers/),
  fetched on 2026-10-12: "To use an authorization request header with ArcGIS services, you can
  use either the Authorization or X-Esri-Authorization HTTP request header." "The word `Bearer`
  must be used before the value of the access token." It calls `X-Esri-Authorization` "a custom
  header defined by Esri, primarily used for ArcGIS services that require web-tier
  authentication", and says that with ArcGIS Server "header tokens are supported starting from
  version 10.5.1". It also says a header keeps intermediaries from reading the token, which a
  query parameter does not.
- [Enable Editor Tracking](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/enable-editor-tracking.html):
  the last editor and last edit date fields.
- [Feature object](https://developers.arcgis.com/rest/services-reference/enterprise/feature-object/):
  "All properties are optional."

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [compressfloor](https://github.com/uhsear/compressfloor) - names what holds the compress
  floor, from exported system tables, and prints an unregister plan for a replica that is
  abandoned. replicakeep repairs the holder that is merely idle. It uses the same exit codes.
- [gdbprune](https://github.com/uhsear/gdbprune) - deletes stale leaf versions and refuses the
  `SYNC_SEND` and `SYNC_RECEIVE` anchors unless you name a flag. replicakeep never deletes a
  version. It lets an anchor move on its own, which is the safe alternative to deleting it.
