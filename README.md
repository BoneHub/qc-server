# BoneHub Dataset Quality Check — Server

Server side of a client–server setup for human-in-the-loop quality check of segmentations
in the [BoneHub Dataset](https://github.com/BoneHub/BoneHub-Dataset).

It points at a folder that is already in BoneHub data structure format and hands subjects
out one at a time to authenticated users. Each user is a **reviewer**, an **editor**, or
both. Reviewers work in the browser, on the server's own
[review page](#reviewing-in-the-browser), which needs no installation but cannot edit: they
accept or reject each label of a subject's segmentation, and report bones it lacks. What
they reject goes to the editors, who correct it in 3D Slicer with the BoneHub extension; a
correction goes back to a reviewer, unless the administrator switched that off.

Nothing reaches the dataset until the administrator approves a subject. Every verdict and
every corrected segmentation waits in the server's own state folder; approving a subject
sets its accepted labels to status `2` ("available, reviewed and corrected") in
`Subject_info_XXX.json`, and moves an editor's correction into the dataset. See
[the quality check](#the-quality-check).

The server runs in Docker; there is no other supported way to run it.

## Features

- A quality check in stages: reviewers judge each label, editors correct what reviewers
  reject, reviewers check the corrections, and the administrator approves. Labels a
  correction leaves alone keep their verdicts; labels it changes are reviewed again.
- Nothing is written into the dataset before the administrator's approval, from the
  [admin panel](#approving-subjects), one subject at a time or all at once.
- Leased assignments: a user holds a subject for a limited time, after which it returns to
  the queue automatically.
- Per-user API keys and [roles](#users-and-roles): a reviewer works on the review page, an
  editor in 3D Slicer, and a user may be both. Keys can be restricted to specific dataset
  ids, and each user has a choice of what they are sent of each subject: the image and its
  segmentation, the segmentation only, or the image only.
- Browser review page at `/review`: a reviewer opens a link, looks at the subject in 3D and
  in slices, and accepts or rejects each label. Everything is rendered in the reviewer's
  browser.
- Browser admin panel at `/admin` for approvals, users and their roles, the queue, the
  audit trail and the policy.
- Validation of every upload (BoneHub `.seg.nrrd` format, every segment a BoneHub label,
  geometry match against the subject's image, size cap). What waits for approval is the
  upload rewritten in canonical form, and the dataset's segmentation is backed up before an
  approval replaces it.
- Audit trail: `submissions.jsonl` and `server.log` in the server's state folder, and a
  human-readable `Dataset_XXX_qualitycheck.log` next to each dataset recording what was
  approved into it.
- Credentials stay inside the container, never on the dataset share.
- Several servers, each with its own admin, can work on one dataset: each keeps its state
  in a folder of its own, and none hands out a subject another one has out or in progress.

## Requirements

- Docker with Compose v2 (Docker Desktop, or Docker Engine on Linux)
- Read-write access to a folder in BoneHub data structure format (`Dataset_001`,
  `Dataset_002`, ...), written with BoneHub data schema 0.3 — see
  [Dataset format](#dataset-format)

## Installation

```bash
git clone https://github.com/BoneHub/bonehub_dataset_quality_check_server.git
cd bonehub_dataset_quality_check_server
cp .env.example .env                          # then say where the dataset is
docker compose up -d --build
docker compose logs | grep -A2 "admin key"    # a new server prints its admin key once
```

Open `http://<host>:8000/admin` and log in with that key. Reviewers use
`http://<host>:8000/review`; editors connect the 3D Slicer extension to
`http://<host>:8000`.

The dataset is on a disk of the Docker host or on an SMB share. Say which in `.env`: fill
in one of the two and leave the other blank.

| Dataset on | Variable | Example |
| --- | --- | --- |
| a disk of the Docker host | `BONEHUB_DATASET_PATH` | `C:/data/BoneHub_Dataset` |
| an SMB share | `BONEHUB_DATASET_SHARE` | `//192.168.0.10/Data/BoneHub/BoneHub_Dataset` |
| | `BONEHUB_SMB_USERNAME` | `alice` |
| | `BONEHUB_SMB_PASSWORD` | the share password |
| | `BONEHUB_SMB_OPTIONS` | `domain=AD,vers=3.0` |

A local folder is bind-mounted; the share is mounted by Docker itself. If both are filled
in, the local folder is used. `docker compose` refuses to start when neither is, or when a
share is given without its username and password.

A mapped network drive is not a local folder. Give its share as a UNC path in
`BONEHUB_DATASET_SHARE`, **not** as a drive letter in `BONEHUB_DATASET_PATH`: Docker
Desktop cannot bind-mount a mapped network drive. Handed `Z:/BoneHub/BoneHub_Dataset` it
creates an empty folder and mounts that instead, so the server starts normally against an
empty dataset and reports `0 of 0 subjects`. `net use` prints the UNC path behind each
mapped drive. A share password containing a comma cannot be used (the comma ends the mount
option), and a literal `$` must be written `$$`.

The share is mounted through a named volume whose options are fixed when it is first
created, so after changing the share or any SMB value recreate it:

```bash
docker compose down && docker volume rm bonehub_dataset_qc_data && docker compose up -d
```

That volume holds no data of its own — only the mount to the share — so nothing is lost.

The server keeps its state in the dataset folder (see
[Where the server keeps things](#where-the-server-keeps-things)). To move a dataset from a
share to a local disk or back, copy its `.bonehub_qc` folder along, or the server finds no
state there and starts on an empty one.

To update the server, pull and rebuild; the server keeps its admin key and users:

```bash
git pull && docker compose up -d --build
```

Update the 3D Slicer extension together with the server; the two share one definition of
the API. The server reads only the state files of its own version: if a new version refuses
to start on the state an older one left, start a new server (`docker compose down -v`, see
[Where the server keeps things](#where-the-server-keeps-things)).

## The quality check

Every subject goes through the same stages. A subject that a user has given a verdict on
has a *case* on the server, which records every verdict, and where the subject stands:

| Stage | Waits for | Handed to |
| --- | --- | --- |
| review | a reviewer's verdict on the labels under review | reviewers |
| edit | a correction: a reviewer rejected a label, or reported one missing | editors |
| approval | the administrator: every label is accepted, or left as it was | nobody |
| escalated | the administrator: an editor could not correct it | nobody |
| approved | — it is in the dataset | nobody, ever again |
| closed | — the administrator closed it; nothing was written | nobody, unless sent back |

A subject nobody has looked at yet goes to the reviewers — every subject with a label of
status `1`, by default — except one without any segmentation, which goes to the editors.
The labels of a subject are each in one state:

| Label | Meaning |
| --- | --- |
| to review | waits for a reviewer: the dataset has it as not reviewed, or an editor changed it |
| accepted | a reviewer accepted it — or its editor, when corrections need no review |
| rejected | a reviewer rejected it: it needs correction, should not be there, or is missing |
| removed | not in the segmentation; it becomes `0`, not available, on approval |
| kept | not under review — the dataset has it as reviewed already — and left as it is |

What each step does:

1. **A reviewer judges the subject**, on the review page. Each label under review is
   accepted or rejected: it *needs correction*, or *should not be there* at all. A bone the
   segmentation lacks is reported *missing*. A reviewer may also reject a label that is not
   under review. With every label accepted, the subject waits for approval; with any label
   rejected or missing, it goes to the editors, and the accepted labels keep their verdicts
   while it is away.
2. **An editor corrects it**, in 3D Slicer, told which labels were rejected and why, and
   what the reviewers wrote. The upload replaces the segmentation under review, and the
   server compares it with the one it replaces, voxel by voxel:
   - a label the upload changed or added, and a label a reviewer had rejected, is
     *corrected*: it goes back to a reviewer — or, with
     [`edits_need_review`](#configuration) off, it is accepted if the editor vouches for it;
   - a label the upload left alone keeps its verdict, so a correction that spills into an
     accepted neighbour takes that neighbour's acceptance away, and one that does not
     leaves it;
   - a label the upload takes away is removed. If a reviewer said it should not be there,
     that is final; otherwise a reviewer must agree first, when corrections need review;
   - a label nobody has reviewed yet stays to be reviewed, whatever the editor vouches for.
3. **A reviewer checks the correction**, which the review page shows in place of the
   dataset's segmentation. The editor is never handed their own correction to review. An
   editor's removal is agreed to by accepting it, and undone by rejecting it as missing.
4. **The administrator approves the subject**, and only now is the dataset written: see
   [Approving subjects](#approving-subjects).

An editor who cannot correct a subject — the image is unusable, say — rejects it with a
comment, and it goes to the administrator. The administrator can send any subject back to
the reviewers (every verdict is reviewed again) or to the editors (with a comment), or
close it without writing anything.

A segmentation that is not on its image's voxel grid cannot be accepted as it is: the review
page starts its labels rejected, and the editor who corrects it writes it back on the grid.

## Administration

### The admin key

A new server generates an admin key and prints it once, in the startup banner, unless
`BONEHUB_QC_ADMIN_KEY` in `.env` chooses one. The key is kept inside the container, never
on the share. If it is lost, print it again from the running container:

```bash
docker compose exec bonehub-qc-server bonehub-qc-server show-admin-key
```

or start a new server, which issues a new admin key (see
[Where the server keeps things](#where-the-server-keeps-things)).

### Users and roles

From the admin panel, or with the server's command line inside the running container:

```bash
docker compose exec bonehub-qc-server bonehub-qc-server add-user --name alice
docker compose exec bonehub-qc-server bonehub-qc-server add-user --name bob --roles reviewer --datasets 1,2
docker compose exec bonehub-qc-server bonehub-qc-server add-user --name carol --roles editor
docker compose exec bonehub-qc-server bonehub-qc-server add-user --name dave --roles reviewer --data-access segmentation
docker compose exec bonehub-qc-server bonehub-qc-server list-users
docker compose exec bonehub-qc-server bonehub-qc-server rotate-key --name alice
```

**Roles.** Each user is a reviewer, an editor, or both, which is the default (`--roles` on
the command line, two tick boxes in the admin panel):

| Role | Works in | Is handed | Can |
| --- | --- | --- | --- |
| Reviewer | the review page, `/review` | subjects waiting for a review | accept or reject each label as it is, report missing bones, reject the whole subject, release |
| Editor | 3D Slicer, with the BoneHub Quality Check extension | subjects a reviewer sent back, and subjects without a segmentation | upload the corrected segmentation, send the subject to the administrator, release |

Each client names the role it works in, on every request, and the server refuses a user who
does not hold it: a reviewer connecting from 3D Slicer is told to use the review page, with
its address, and an editor cannot sign in to the review page. A user with both roles can use
both, and holds up to `max_concurrent_assignments_per_user` subjects in each; a subject is
submitted in the role it was handed out in. What a role may do is checked against the
account itself, whichever client a request comes from: only an editor uploads a
segmentation, and only a reviewer judges one as it is. Roles can be changed in the Users
table at any time and apply from the user's next request; a user needs at least one.

The API key is shown once, at creation. For a reviewer the admin panel shows it together with
an **invite link**, `http://<host>:8000/review#key=bhqc_...`, which signs them in to the
review page by itself. The part after `#` never reaches the server, so the key stays out of
its logs; the link is the credential all the same, so send it privately. For an editor, hand
over the key and the server URL, which they enter in the 3D Slicer extension. The running
server sees a user added from the command line at once.

**What a user is sent** is set per user when creating them, and can be changed in the Users
table at any time (`--data-access` on the command line):

| Setting | Sent | They can |
| --- | --- | --- |
| Image + segmentation (default) | both | whatever their roles allow |
| Segmentation only | the segmentation | as a reviewer, judge the labels without the image. Not handed subjects without a segmentation |
| Image only | the image | as a reviewer, reject the subject with a comment or report missing bones, but not accept a label they have not seen; as an editor, create a segmentation for a subject that has none |

A file a user is not sent is left out of their handout and refused at its download endpoint,
whichever client asks. Every verdict records the user's setting in the audit trail.

### Approving subjects

The **Approvals** section of the admin panel lists the subjects in progress, by stage —
those waiting for approval first. Each shows every label with its state and who stands
behind it ("accepted by rita, corrected by eddie"), whether the segmentation is the
dataset's own or an editor's correction — which **Download** saves, to look at in 3D Slicer —
and the latest step with its comment.

**Approve** writes one subject into the dataset; **Approve all waiting** writes every subject
waiting for approval, and reports any it could not. Approving:

- sets each accepted label to `2` in `Subject_info_XXX.json`, and each label no longer in the
  segmentation to `0` (with `mark_removed_labels_absent`, the default);
- backs up the dataset's segmentation into the server's `backups/` folder, and moves the
  editor's correction into the dataset, if there is one;
- writes a line into `Dataset_XXX_qualitycheck.log` saying who accepted and who corrected
  each label.

An approval is refused when the dataset's segmentation changed after the subject's quality
check began — another tool or another server wrote it — since approving would overwrite
that; send the subject back to review instead. It is refused too when the dataset was
regenerated under another schema. If `Subject_info` cannot be written, the segmentation is put
back, so no subject is left half approved.

**To reviewers**, **To editors** and **Close** are there for every subject that is not approved
yet, and for a closed one, which they reopen; they wait while a user holds the subject.

### Other commands

```bash
docker compose exec bonehub-qc-server bonehub-qc-server stats         # the queue, by stage
docker compose exec bonehub-qc-server bonehub-qc-server show-config   # the effective policy
docker compose exec bonehub-qc-server bonehub-qc-server sessions      # every server of this dataset
docker compose logs -f                                                 # the server's output
```

Endpoints:

| URL | What it is |
| --- | --- |
| `/review` | Review page (asks for a reviewer's API key) |
| `/admin` | Admin panel (asks for the admin key) |
| `/docs` | Interactive OpenAPI documentation |
| `/health` | Unauthenticated liveness probe |
| `/api/v1/...` | Client API, authenticated with `X-API-Key`, in the role named by `X-Client-Role` |
| `/admin/api/...` | Admin API, authenticated with `X-Admin-Key`; `cases` holds the approvals |
| `/static/...` | The pages' script and the vendored NiiVue viewer |

## Reviewing in the browser

The review page at `/review` is for reviewers, who only need to look: nothing to install, and
all rendering happens in the reviewer's browser with [NiiVue](https://github.com/niivue/niivue),
so the server only sends files. A user who is not a reviewer is refused at sign-in. A reviewer:

1. opens the invite link, or `/review` and enters their key. "Remember" keeps the key in this
   browser; otherwise it is forgotten when the tab closes;
2. presses **Get next subject**. A subject they already hold, for instance after closing the
   tab, is opened again by itself. A subject that has been through an editor says so, shows
   the editor's correction, and lists what happened to it so far, with the comments;
3. looks at it. The 3D view renders the labels, and the slices show the image with the labels
   over it. Clicking a label in the list moves the crosshair onto that bone, the eye hides it,
   and the target shows it alone. **Outline**, **Distinct colours** (neighbouring bones in
   clearly different colours, instead of the dataset's own), a CT window and a single-plane
   view help with the details;
4. gives each label under review a verdict: ✓ accepts it, ✗ rejects it, and a rejected
   label takes a reason, *needs correction* or *should not be there*. Every label under
   review starts accepted. A bone the segmentation lacks is reported under **A bone the
   segmentation lacks**. A comment says what is wrong;
5. presses **Accept** — or **Send to editors**, when something is rejected or missing — or
   **Reject subject**, which rejects every label under review, or **Release**.

The page cannot edit, and nothing it sends reaches the dataset before the administrator
approves the subject. Corrections are made in 3D Slicer, by an editor.

A segmentation that is not on its image's voxel grid cannot be accepted as it is. The server
holds it to the same geometry check as an upload (`require_geometry_match`), so the page says
so and starts its labels rejected, for an editor to write it back on the image's grid.

**Large scans.** The browser needs several copies of a volume in GPU memory, and in testing a
450-million-voxel whole-body CT would not display at full resolution. The page therefore
shows at most 256 million voxels in the slices, and 64 million in the 3D view, by leaving out
every second voxel along the finest axes (for example 0.6 mm slices shown at 1.2 mm). It says
so above the labels, and adds a note to the comment of a verdict given on such a view. On
the machine it was tested on, a 150–450-million-voxel CT took 15–25 seconds to open once
downloaded, and 1.5–3 GB of browser memory; a small scan opens in a few seconds.

**Browsers.** A current Chrome, Edge, Firefox or Safari with WebGL 2. Use HTTPS in front of
the server when users connect over anything but a trusted network: the API key and the
images travel in every request.

NiiVue is vendored as one self-contained file in
`bonehub_quality_check_server/static/vendor/` (BSD-2-Clause; its license is next to it), so
the page works on networks that cannot reach a CDN. To update it:

```bash
python tools/update_niivue.py 0.69.0     # downloads, checks and unpacks that version
```

then point the import at the top of `static/review.js` at the new file, delete the old one,
and run the tests. The page uses one NiiVue internal (`refreshLayers`), so check it after an
update.

## Where the server keeps things

**Credentials** — the server's id, its private key, the admin key, and the user accounts
with their roles and key digests — are kept inside the container, in `/var/lib/bonehub-qc`,
which docker-compose mounts from the `bonehub_qc_credentials` volume on the Docker host.
They are never written to the dataset share, and the server refuses to start if its
credentials folder is inside the dataset.

**Everything else** is on the share, in a folder of the server's own, named after its id:

```
<dataset-root>/.bonehub_qc/<server id>/
├── session.json          which server this is, when it was created and last started
├── config.json           quality-check policy
├── assignments.json      open and finished leases
├── cases.json            the subjects in progress: every verdict so far, and where each stands
├── cases_done.jsonl      the subjects approved or closed, one line each
├── staged/               editors' corrected segmentations (.seg.nrrd), waiting for approval
├── submissions.jsonl     append-only audit trail
├── server.log            server lifecycle, verdicts and administrative events
├── backups/              the dataset's segmentations (.seg.nrrd), kept before an approval replaced them
└── tmp/                  uploads being validated
```

The credentials volume *is* the server:

| You run | What happens |
| --- | --- |
| `docker compose up -d`, `restart`, `up -d --build` | Same server: same admin key, users and state folder |
| `docker compose down -v`, then `up -d` | A **new** server: a new id and state folder, a new admin key (printed once), no users. The old server's folder stays on the share as history |

Several servers, each with its own admin, can therefore work on one dataset — from
different machines, or one after the other — without overwriting each other: each has its
own state folder, and a server does not hand out a subject that another one has out, or in
progress. A subject in progress on another server carries verdicts that wait for that
server's administrator and are not in the dataset yet, so it is left to that server until
its administrator approves or closes it. `bonehub-qc-server sessions` lists the servers.
Changing `BONEHUB_QC_PRIVATE_KEY` invalidates every API key already issued.

## Dataset format

The server follows [BoneHub data schema](https://github.com/BoneHub/BoneHub-Dataset) 0.3:

- **Segmentations** are `Segmentation/<dataset>_<subject>.seg.nrrd`: voxels hold per-file
  segment numbers, and the header maps each number to its `BoneLabelMap` label (nine-digit
  values built from structure, part, tissue and side). Segmentations travel between server
  and client in this format both ways.
- **Label statuses** in `Subject_info_XXX.json`:

  | Status | Meaning | Queued for review by default |
  | --- | --- | --- |
  | `0` | not available (same as the label being absent) | no — nothing to review |
  | `1` | available, not reviewed or corrected | yes |
  | `2` | available, reviewed and corrected (if necessary) | no; add it to `eligible_label_values` for a second review |

- **Schema version.** Each `Dataset_info_XXX.json` records the `schema_version` it was
  written with. A dataset of another major.minor version — or one that records none — is
  skipped, with the reason in `server.log`; regenerate it with the current converters. The
  server itself refuses to start if the installed `bonehub_data_schema` is not 0.3.x.

What an approved subject does to each label of `Subject_info_XXX.json`:

| Label | New status |
| --- | --- |
| Accepted — by a reviewer, or by its editor when corrections need no review | `2` |
| In the segmentation but not under review (kept) | unchanged; `1` if it was absent or `0` |
| No longer in the segmentation | `0` (with `mark_removed_labels_absent`, the default) |

A label is under review when its status is one of `eligible_label_values`, and also when the
segmentation paints it while `Subject_info` lists it as not available, or not at all.

## Client flow

Every request carries the user's key in `X-API-Key` and the role the client works in in
`X-Client-Role`: `editor` from the 3D Slicer extension, `reviewer` from the review page. A
request without the role, or in a role the user does not hold, is refused (400 and 403). The
two clients follow the same sequence:

1. `GET /api/v1/ping` — check the key and its role; reports the server's `schema_version`,
   the user's `roles`, the `role` of this request, the user's `data_access`, and whether
   corrections go back to a reviewer (`edits_need_review`)
2. `GET /api/v1/labels` — the label map, the label statuses and the reasons to reject a label
3. `POST /api/v1/subjects/next` — lease the next subject for this role
4. `GET /api/v1/assignments/{id}/image` — download the image (`.nii.gz`)
5. `GET /api/v1/assignments/{id}/segmentation` — download the segmentation under review
   (`.seg.nrrd`): an editor's correction waiting for approval, or the dataset's own
6. `POST /api/v1/assignments/{id}/submit` — send the verdict back, as multipart with a
   `metadata` part:
   - a reviewer: `quality_check_confirmed: true`, `use_stored_segmentation: true`,
     `confirmed_labels` (accepted), `rejected_labels` (label → `quality`, `absent`, or
     `missing` for one not in the segmentation), `missing_labels`, `comment`. No file.
     `quality_check_confirmed: false` rejects every label under review;
   - an editor: `quality_check_confirmed: true`, the corrected `.seg.nrrd` as the
     `segmentation` part, `confirmed_labels` (vouched for), `comment`.
     `quality_check_confirmed: false` sends the subject to the administrator.

   The response says where the subject went (`stage`), what the verdict accepted, rejected,
   corrected and removed, which labels now wait for a reviewer, and a message for the user.

The handout says what there is for this user to download (`has_image`,
`has_segmentation`, and a URL for each), which follows their `data_access`, and whether the
segmentation is an editor's correction (`segmentation_source`). It carries the subject's
`stage`, every label with its state, reason and who gave it (`labels`), open requests from the
administrator (`requests`), and the subject's quality check so far with its comments
(`history`). With the segmentation it also carries `segments`, read from the file header: each
segment's number, BoneHub label and value, colour and bounding box, and
`stored_segmentation_issue`: why the segmentation could not be accepted as it is, if there is a
reason.

[`client.py`](bonehub_quality_check_server/client.py) is a dependency-free reference client
for the same API; it is the file shipped inside the Slicer extension. It works as an editor,
unless it is given `role="reviewer"`:

```python
from pathlib import Path
from bonehub_quality_check_server.client import BoneHubQCClient

editor = BoneHubQCClient("http://localhost:8000", "bhqc_...")
handout = editor.next_subject()
editor.download_image(handout["assignment_id"], Path("image.nii.gz"))
editor.download_segmentation(handout["assignment_id"], Path("segmentation.seg.nrrd"))
# ... correct in 3D Slicer ...
editor.submit(handout["assignment_id"], quality_check_confirmed=True,
              segmentation_path=Path("corrected.seg.nrrd"))

reviewer = BoneHubQCClient("http://localhost:8000", "bhqc_...", role="reviewer")
handout = reviewer.next_subject()
reviewer.submit(handout["assignment_id"], quality_check_confirmed=True, use_stored_segmentation=True,
                confirmed_labels=["FEMUR_LEFT"], rejected_labels={"FEMUR_RIGHT": "quality"})
```

An upload must be a single-layer `.seg.nrrd` on the image's voxel grid, and every segment
must resolve to a BoneHub label — through its `BoneHubValue` tag, else its name, as
`bonehub_data_schema.read_segmentation` reads it. Anything else is refused with a message
naming the problem.

## Configuration

The policy is set in `.env`, as one `BONEHUB_QC_<FIELD>` variable per setting, and can be
changed at runtime from the admin panel. The server stores it in its `config.json`; a
variable set in `.env` wins over the stored value at every start. The most used settings:

| Setting | Default | Meaning |
| --- | --- | --- |
| `allowed_dataset_ids` | `null` | Restrict the server to these dataset ids; `null` means every dataset under the root |
| `eligible_label_values` | `[1]` | Label statuses that queue a subject for review (`1` not reviewed, `2` reviewed) |
| `edits_need_review` | `true` | Send the labels an editor corrected back to a reviewer; `false` accepts those the editor vouches for |
| `include_subjects_without_segmentation` | `false` | Also queue subjects with an image but no available label, for editors to segment from scratch |
| `mark_removed_labels_absent` | `true` | On approval, set a label no longer in the segmentation to `0` |
| `lease_ttl_seconds` | `86400` | How long a user keeps a subject |
| `max_concurrent_assignments_per_user` | `1` | Subjects one user may hold at once, in each role |
| `assignment_strategy` | `sequential` | `sequential` or `random` handout order |
| `require_geometry_match` | `true` | Refuse a segmentation whose voxel grid differs from the image |
| `keep_segmentation_backups` | `true` | Back up the dataset's segmentation before an approval replaces it |
| `max_upload_bytes` | `536870912` | Largest accepted segmentation upload |

See [`.env.example`](.env.example) for the variables and
[`config.py`](bonehub_quality_check_server/config.py) for the full list. A setting that
`.env` does not name is added to the `environment:` block of `docker-compose.yml` the same
way as the others.

## Tests

The suite is plain `unittest` and runs in the server's own image, so it needs nothing but
Docker. It builds a throw-away dataset in BoneHub data structure format under a temporary
folder for every test, so it never touches a real dataset.

```bash
docker build -t bonehub-qc-server .
docker run --rm -v "${PWD}:/src" -w /src bonehub-qc-server \
    sh -c "pip install -q httpx && python -m unittest discover -s tests -t ."
```

To run one module or one test, replace the last command, for example with
`python -m unittest tests.test_workflow -v`.

| Module | What it covers |
| --- | --- |
| `test_workflow.py` | The stages: reviewers first, per-label verdicts and missing bones, editors' corrections compared voxel by voxel, removals, review after correction or not, nobody reviewing their own correction, leases per role, late verdicts |
| `test_approval.py` | What approving writes into the dataset, and nothing before it; refusals, backups and the undoing of a failed write; approving all; sending back and closing; the admin API for it |
| `test_submission.py` | What an editor's upload is held to: the `.seg.nrrd` format, its canonical form, validation; rejections changing nothing; uploads of some bones only; the audit trail |
| `test_confirm_as_is.py` | Judging the stored segmentation as it is, the geometry check on it, and who may accept or replace a segmentation |
| `test_config.py` | The policy file and its `BONEHUB_QC_*` overrides |
| `test_auth.py` | Server id, private key, per-user API keys, disabling and rotation, accounts changed from the CLI |
| `test_roles.py` | Reviewers and editors: the roles of an account, which client each role may use, what each may submit, the queue each is handed, the admin panel, the CLI and the reference client |
| `test_sessions.py` | Credentials kept off the share, the admin key printed once, several servers on one dataset |
| `test_queue.py` | Which subjects are queued, schema versions, restricting the server to specific datasets, broken dataset folders |
| `test_assignment.py` | Who gets which subject, leases, expiry, release, where a judged subject goes, the queue by stage |
| `test_data_access.py` | What a user is sent of each subject, in the handout, the downloads, the queue, the admin panel and the CLI |
| `test_review_page.py` | The review page's files, what is installed with the package, the vendored NiiVue build, the segment table in the handout, and the names the pages share with the server |
| `test_api.py` | The REST API over HTTP, as both clients call it, from first review to approval |
| `test_admin.py` | The admin panel endpoints behind the admin key |
| `test_client.py` | `client.py` against a real uvicorn server on a real socket, in both roles |
| `test_cli.py` | `bonehub-qc-server` commands |
| `test_concurrency.py` | Several users hitting the server at once, and other users answered while one submission is checked or an approval is written |
| `test_deployment.py` | Start-up from environment variables only, the credentials volume, the shipped docker files, and — where the docker CLI is at hand — what Compose makes of a local dataset folder or a share |

`tests/support.py` holds the dataset builder, the base test case, and one-line steps of the
workflow (`review`, `edit`).

## License

See [LICENSE](LICENSE).
