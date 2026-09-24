# BoneHub Dataset Quality Check — Server

Server side of a client–server setup for human-in-the-loop quality check of segmentations
in the [BoneHub Dataset](https://github.com/BoneHub/BoneHub-Dataset).

It points at a folder that is already in BoneHub data structure format, hands subjects out
one at a time to authenticated users, and receives their verdicts back. Each user is a
**reviewer**, an **editor**, or both. Reviewers work in the browser, on the server's own
[review page](#reviewing-in-the-browser), which needs no installation but cannot edit.
Editors work in 3D Slicer with the BoneHub extension, where they can correct the
segmentation. When a subject is confirmed, the labels vouched for are set to status `2`
("available, reviewed and corrected") in `Subject_info_XXX.json`, whatever status they had
before, and a corrected segmentation from 3D Slicer replaces the one in the dataset.
Rejected subjects leave the dataset untouched and are only recorded in the audit trail.

The server runs in Docker; there is no other supported way to run it.

## Features

- Leased assignments: a user holds a subject for a limited time, after which it returns to
  the queue automatically.
- Per-user API keys and [roles](#users-and-roles): a reviewer works on the review page, an
  editor in 3D Slicer, and a user may be both. Keys can be restricted to specific dataset
  ids, and each user has a choice of what they are sent of each subject: the image and its
  segmentation, the segmentation only, or the image only.
- Browser review page at `/review`: a reviewer opens a link, looks at the subject in 3D and
  in slices, and confirms or rejects it. Everything is rendered in the reviewer's browser.
- Browser admin panel at `/admin` for users and their roles, queue statistics, submissions
  and config.
- Validation of every upload (BoneHub `.seg.nrrd` format, every segment a BoneHub label,
  geometry match against the subject's image, size cap). The dataset receives the upload
  rewritten in canonical form, and the previous segmentation is backed up first.
- Audit trail: `submissions.jsonl`, `server.log`, and a human-readable
  `Dataset_XXX_qualitycheck.log` next to each dataset.
- Credentials stay inside the container, never on the dataset share.
- Several servers, each with its own admin, can work on one dataset: each keeps its state
  in a folder of its own, and none hands out a subject another one has leased.

## Requirements

- Docker with Compose v2 (Docker Desktop, or Docker Engine on Linux)
- Read-write access to a folder in BoneHub data structure format (`Dataset_001`,
  `Dataset_002`, ...), written with BoneHub data schema 0.3 — see
  [Dataset format](#dataset-format)

## Installation

```bash
git clone https://github.com/BoneHub/bonehub_dataset_quality_check_server.git
cd bonehub_dataset_quality_check_server
cp .env.example .env                          # then set the share and its credentials
docker compose up -d --build
docker compose logs | grep -A2 "admin key"    # a new server prints its admin key once
```

Open `http://<host>:8000/admin` and log in with that key. Reviewers use
`http://<host>:8000/review`; editors connect the 3D Slicer extension to
`http://<host>:8000`.

The dataset is mounted over SMB, configured by four values in `.env`:

| Variable | Example |
| --- | --- |
| `BONEHUB_DATASET_SHARE` | `//192.168.0.10/Data/BoneHub/BoneHub_Dataset` |
| `BONEHUB_SMB_USERNAME` | `alice` |
| `BONEHUB_SMB_PASSWORD` | the share password |
| `BONEHUB_SMB_OPTIONS` | `domain=AD,vers=3.0` |

Give `BONEHUB_DATASET_SHARE` as a UNC path, **not** as a Windows drive letter. Docker
Desktop cannot bind-mount a mapped network drive: handed `Z:/BoneHub/BoneHub_Dataset` it
creates an empty folder and mounts that instead, so the server starts normally against an
empty dataset and reports `0 of 0 subjects`. `net use` prints the UNC path behind each
mapped drive. A password containing a comma cannot be used (the comma ends the mount
option), and a literal `$` must be written `$$`.

The share is mounted through a named volume whose options are fixed when it is first
created, so after changing any of the four values recreate it:

```bash
docker compose down && docker volume rm bonehub_dataset_qc_data && docker compose up -d
```

That volume holds no data of its own — only the mount to the share — so nothing is lost.
For a dataset on a local disk, [`docker-compose.yml`](docker-compose.yml) ends with the
bind-mount alternative.

To update the server, pull and rebuild; the server keeps its admin key and users:

```bash
git pull && docker compose up -d --build
```

Update the 3D Slicer extension together with the server: since 0.3 every client names its
role in each request, and the server refuses an extension from before that with a message
saying to update it.

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

| Role | Works in | Can |
| --- | --- | --- |
| Reviewer | the review page, `/review` | look at a subject, confirm the stored segmentation as it is, reject, release. Is not handed subjects without a segmentation |
| Editor | 3D Slicer, with the BoneHub Quality Check extension | correct the segmentation and upload it, or create one for a subject that has none; reject, release |

Each client names the role it works in, and the server refuses a user who does not hold it,
on every request: a reviewer connecting from 3D Slicer is told to use the review page, with
its address, and an editor cannot sign in to the review page. A user with both roles can use
both. What a role may do to the dataset is checked against the account itself, whichever
client a request comes from: only an editor uploads a segmentation, and only a reviewer
confirms the stored one as it is. Roles can be changed in the Users table at any time and
apply from the user's next request; a user needs at least one. Users created before server
0.3 hold both roles.

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
| Segmentation only | the segmentation | as a reviewer, look at the labels without the image, confirm, reject. Not given subjects without a segmentation. Of no use to an editor: 3D Slicer needs the image |
| Image only | the image | reject with a comment, or release. Cannot confirm or replace a segmentation they have not seen, so as an editor they can only create one for a subject that has none |

A file a user is not sent is left out of their handout and refused at its download endpoint,
whichever client asks. Every verdict records the user's setting in the audit trail.

### Other commands

```bash
docker compose exec bonehub-qc-server bonehub-qc-server stats         # the queue
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
| `/static/...` | The pages' script and the vendored NiiVue viewer |

## Reviewing in the browser

The review page at `/review` is for reviewers, who only need to look: nothing to install, and
all rendering happens in the reviewer's browser with [NiiVue](https://github.com/niivue/niivue),
so the server only sends files. A user who is not a reviewer is refused at sign-in. A reviewer:

1. opens the invite link, or `/review` and enters their key. "Remember" keeps the key in this
   browser; otherwise it is forgotten when the tab closes;
2. presses **Get next subject**. A subject they already hold, for instance after closing the
   tab, is opened again by itself;
3. looks at it. The 3D view renders the labels, and the slices show the image with the labels
   over it. Clicking a label in the list moves the crosshair onto that bone, the eye hides it,
   and the target shows it alone. **Outline**, **Distinct colours** (neighbouring bones in
   clearly different colours, instead of the dataset's own), a CT window and a single-plane
   view help with the details;
4. ticks the labels they vouch for, writes a comment if something is worth recording, and
   presses **Confirm**, **Reject** or **Release**.

The page cannot edit. **Confirm** vouches for the stored segmentation as it is: the ticked
labels are set to `2`, and the file is left untouched (`use_stored_segmentation` in the API).
A rejection records the verdict and comment, and changes nothing. Corrections are made in 3D
Slicer, by an editor.

A stored segmentation that is not on its image's voxel grid cannot be confirmed as it is.
The server holds it to the same geometry check as an upload (`require_geometry_match`), so
the page says so and offers Reject instead. An editor correcting the subject in 3D Slicer
writes the segmentation back on the image's grid.

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
├── config.json           quality-check policy, with the schema version its statuses belong to
├── assignments.json      open and finished assignments
├── submissions.jsonl     append-only audit trail
├── server.log            server lifecycle and administrative events
├── backups/              previous segmentations (.seg.nrrd), kept before overwriting
└── tmp/                  uploads being validated
```

The credentials volume *is* the server:

| You run | What happens |
| --- | --- |
| `docker compose up -d`, `restart`, `up -d --build` | Same server: same admin key, users and state folder |
| `docker compose down -v`, then `up -d` | A **new** server: a new id and state folder, a new admin key (printed once), no users. The old server's folder stays on the share as history |

Several servers, each with its own admin, can therefore work on one dataset — from
different machines, or one after the other — without overwriting each other: each has its
own state folder, and a server does not hand out a subject that another one has out for
review. `bonehub-qc-server sessions` lists them. Changing `BONEHUB_QC_PRIVATE_KEY`
invalidates every API key already issued.

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

What a confirmed submission does to each label. For a confirmation of the stored
segmentation as it is, read "the stored segmentation" for "the upload":

| Label | New status |
| --- | --- |
| In the upload and vouched for by the user | `2` |
| In the upload, not vouched for | unchanged; `1` if it was absent or `0` |
| In the dataset but no longer in the upload | `0` (with `mark_removed_labels_absent`, the default) |

### Upgrading from server 0.2

Server 0.2 knew no roles: every user could work in both clients. The users it created are
kept, as reviewers and editors both, so nobody is locked out; take away the role a user
should not have in the Users table. Every client now names its role in each request, so
update the 3D Slicer extension along with the server. The review page is served by the
server itself and needs nothing.

### Upgrading from server 0.1

Server 0.1 used the pre-0.3 label statuses (`-1`…`3`) and NIfTI segmentations, and kept
everything — its keys and user accounts included — directly in `<dataset-root>/.bonehub_qc/`.
The new server reads none of those files:

- Regenerate the datasets with the schema 0.3 converters; the server skips the others.
- Delete `server_private_key`, `admin_key` and `users.json` from
  `<dataset-root>/.bonehub_qc/`. The server warns at every start while they are there, but
  deletes nothing on the share itself. The old `assignments.json`, `submissions.jsonl`,
  `server.log` and `backups/` can stay as history.
- Users need new keys from the new server.
- `BONEHUB_QC_ELIGIBLE_LABEL_VALUES` in `.env` is read in the new statuses: `1` queues the
  subjects nobody has reviewed yet.

## Client flow

Every request carries the user's key in `X-API-Key` and the role the client works in in
`X-Client-Role`: `editor` from the 3D Slicer extension, `reviewer` from the review page. A
request without the role, or in a role the user does not hold, is refused (400 and 403). The
two clients follow the same sequence:

1. `GET /api/v1/ping` — check the key and its role; reports the server's `schema_version`,
   the user's `roles`, the `role` of this request, and the user's `data_access`
2. `GET /api/v1/labels` — the label map and label statuses
3. `POST /api/v1/subjects/next` — lease the next subject
4. `GET /api/v1/assignments/{id}/image` — download the image (`.nii.gz`)
5. `GET /api/v1/assignments/{id}/segmentation` — download the segmentation (`.seg.nrrd`), if any
6. `POST /api/v1/assignments/{id}/submit` — send the verdict back: an editor with the
   corrected `.seg.nrrd`, a reviewer with `use_stored_segmentation: true` in the metadata to
   confirm the stored one as it is

The handout says what there is for this user to download (`has_image`,
`has_segmentation`, and a URL for each), which follows their `data_access`. With the
segmentation it also carries `segments`, read from the file header: each segment's number,
BoneHub label and value, colour and bounding box. It also carries
`stored_segmentation_issue`: why the stored segmentation could not be confirmed as it is, if
there is a reason.

[`client.py`](bonehub_quality_check_server/client.py) is a dependency-free reference client
for the same API; it is the file shipped inside the Slicer extension. It works as an editor,
unless it is given `role="reviewer"`:

```python
from pathlib import Path
from bonehub_quality_check_server.client import BoneHubQCClient

client = BoneHubQCClient("http://localhost:8000", "bhqc_...")
handout = client.next_subject()
client.download_image(handout["assignment_id"], Path("image.nii.gz"))
client.download_segmentation(handout["assignment_id"], Path("segmentation.seg.nrrd"))
# ... correct in 3D Slicer ...
client.submit(handout["assignment_id"], quality_check_confirmed=True,
              segmentation_path=Path("reviewed.seg.nrrd"))
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
| `eligible_label_values` | `[1]` | Label statuses that queue a subject (`1` not reviewed, `2` reviewed) |
| `include_subjects_without_segmentation` | `false` | Also queue subjects with an image but no available label, for editors to segment from scratch |
| `mark_removed_labels_absent` | `true` | Set a label deleted by the editor to `0` |
| `lease_ttl_seconds` | `86400` | How long a user keeps a subject |
| `max_concurrent_assignments_per_user` | `1` | Subjects one user may hold at once |
| `assignment_strategy` | `sequential` | `sequential` or `random` handout order |
| `requeue_rejected` | `false` | Hand rejected subjects out again |
| `require_geometry_match` | `true` | Reject uploads whose voxel grid differs from the image |
| `keep_segmentation_backups` | `true` | Back up a segmentation before overwriting it |
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
`python -m unittest tests.test_submission -v`.

| Module | What it covers |
| --- | --- |
| `test_config.py` | The policy file, its `BONEHUB_QC_*` overrides, and a policy stored under another schema |
| `test_auth.py` | Server id, private key, per-user API keys, disabling and rotation, accounts changed from the CLI |
| `test_roles.py` | Reviewers and editors: the roles of an account, which client each role may use, how each may confirm, the queue each is handed, the admin panel, the CLI and the reference client |
| `test_sessions.py` | Credentials kept off the share, the admin key printed once, several servers on one dataset |
| `test_queue.py` | Which subjects are queued, schema versions, restricting the server to specific datasets, broken dataset folders |
| `test_assignment.py` | Who gets which subject, leases, expiry, release, requeue policy |
| `test_submission.py` | Confirmed submissions marking labels reviewed (2), the `.seg.nrrd` format and its validation, rejections changing nothing, partial uploads, audit trail |
| `test_confirm_as_is.py` | Confirming the stored segmentation as it is, the geometry check on it, and who may confirm or replace a segmentation |
| `test_data_access.py` | What a user is sent of each subject, in the handout, the downloads, the queue, the admin panel and the CLI |
| `test_review_page.py` | The review page's files, what is installed with the package, the vendored NiiVue build, and the segment table in the handout |
| `test_api.py` | The REST API over HTTP, as the 3D Slicer extension calls it |
| `test_admin.py` | The admin panel endpoints behind the admin key |
| `test_client.py` | `client.py` against a real uvicorn server on a real socket |
| `test_cli.py` | `bonehub-qc-server` commands |
| `test_concurrency.py` | Several users hitting the server at once, and other users answered while one submission is checked and written |
| `test_deployment.py` | Start-up from environment variables only, the credentials volume, and the shipped docker files |

`tests/support.py` holds the dataset builder and the base test case.

## License

See [LICENSE](LICENSE).
