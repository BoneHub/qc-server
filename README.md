# BoneHub Dataset Quality Check — Server

Server side of a client–server setup for human-in-the-loop quality check of segmentations
in the [BoneHub Dataset](https://github.com/BoneHub/BoneHub-Dataset).

It points at a folder that is already in BoneHub data structure format, hands subjects out
one at a time to authenticated reviewers working in 3D Slicer, and receives the reviewed
segmentations back. When a reviewer confirms a subject, the uploaded segmentation replaces
the one in the dataset and the labels the reviewer vouches for are set to status `2`
("available, reviewed and corrected") in `Subject_info_XXX.json`, whatever status they
had before. Rejected subjects leave the dataset untouched and are only recorded in the
audit trail.

## Features

- Leased assignments: a reviewer holds a subject for a limited time, after which it returns
  to the queue automatically.
- Per-reviewer API keys, optionally restricted to specific dataset ids.
- Browser admin panel at `/admin` for reviewers, queue statistics, submissions and config.
- Validation of every upload (BoneHub `.seg.nrrd` format, every segment a BoneHub label,
  geometry match against the subject's image, size cap). The dataset receives the upload
  rewritten in canonical form, and the previous segmentation is backed up first.
- Audit trail: `submissions.jsonl`, `server.log`, and a human-readable
  `Dataset_XXX_qualitycheck.log` next to each dataset.
- All server state lives inside the dataset folder (`.bonehub_qc/`), so a dataset carries
  its own quality-check policy and history with it.

## Requirements

- Python 3.10+ (or Docker)
- Read-write access to a folder in BoneHub data structure format (`Dataset_001`,
  `Dataset_002`, ...), written with BoneHub data schema 0.3 — see
  [Dataset format](#dataset-format)

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

What a confirmed submission does to each label:

| Label | New status |
| --- | --- |
| In the upload and vouched for by the reviewer | `2` |
| In the upload, not vouched for | unchanged; `1` if it was absent or `0` |
| In the dataset but no longer in the upload | `0` (with `mark_removed_labels_absent`, the default) |

### Upgrading from server 0.1

Server 0.1 used the pre-0.3 label values (`-1`…`3`) and NIfTI segmentations. The
`config.json` it left in `.bonehub_qc/` is upgraded on the first start and the change is
logged: `confirmed_label_value` is dropped (confirmed labels are always `2`), and
`eligible_label_values` is translated (old `2`, "generated, without quality check", becomes
`1`). `BONEHUB_QC_ELIGIBLE_LABEL_VALUES` in `.env` is **not** translated — it is read in the
new statuses, so `1` is the value that queues unreviewed subjects.

## Installation

### Docker (recommended)

```bash
git clone https://github.com/BoneHub/bonehub_dataset_quality_check_server.git
cd bonehub_dataset_quality_check_server
cp .env.example .env          # then set the share and your credentials
docker compose up -d
docker compose logs | grep -A2 "admin key"   # the first start prints the admin key
```

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

The volume holds no data of its own — only the mount to the share — so nothing is lost.
For a dataset on a local disk, [`docker-compose.yml`](docker-compose.yml) ends with the
bind-mount alternative.

### From source

```bash
git clone https://github.com/BoneHub/bonehub_dataset_quality_check_server.git
cd bonehub_dataset_quality_check_server
pip install .            # pip install -e ".[dev]" for development
```

## Usage

### Run the server

```bash
bonehub-qc-server serve --dataset-root Z:/BoneHub/BoneHub_Dataset --port 8000
```

`--dataset-root` may also be given as `BONEHUB_QC_DATASET_ROOT`. The same commands are
available as `python -m bonehub_quality_check_server <command>`.

On the first start against a dataset folder, an admin key is generated and printed in the
startup banner; it is stored in `<dataset-root>/.bonehub_qc/admin_key`. Set
`BONEHUB_QC_ADMIN_KEY` to choose your own instead.

Endpoints once it is up:

| URL | What it is |
| --- | --- |
| `/admin` | Admin panel (asks for the admin key) |
| `/docs` | Interactive OpenAPI documentation |
| `/health` | Unauthenticated liveness probe |
| `/api/v1/...` | Client API, authenticated with `X-API-Key` |

### Add reviewers

From the admin panel, or from the command line:

```bash
bonehub-qc-server add-user       --dataset-root Z:/... --name alice
bonehub-qc-server add-user       --dataset-root Z:/... --name bob --datasets 1,2
bonehub-qc-server list-users     --dataset-root Z:/...
bonehub-qc-server rotate-key     --dataset-root Z:/... --name alice
bonehub-qc-server show-admin-key --dataset-root Z:/...
bonehub-qc-server stats          --dataset-root Z:/...
bonehub-qc-server show-config    --dataset-root Z:/...
```

The reviewer API key is shown once, at creation. Hand it to the reviewer together with the
server URL; they enter both in the 3D Slicer extension.

### Client flow

Reviewers normally use the 3D Slicer extension, which follows this sequence:

1. `GET /api/v1/ping` — check the key; reports the server's `schema_version`
2. `GET /api/v1/labels` — the label map and label statuses
3. `POST /api/v1/subjects/next` — lease the next subject
4. `GET /api/v1/assignments/{id}/image` — download the image (`.nii.gz`)
5. `GET /api/v1/assignments/{id}/segmentation` — download the segmentation (`.seg.nrrd`), if any
6. `POST /api/v1/assignments/{id}/submit` — send the verdict back, with the reviewed `.seg.nrrd`

[`client.py`](bonehub_quality_check_server/client.py) is a dependency-free reference client
for the same API (it is the file shipped inside the Slicer extension):

```python
from pathlib import Path
from bonehub_quality_check_server.client import BoneHubQCClient

client = BoneHubQCClient("http://localhost:8000", "bhqc_...")
handout = client.next_subject()
client.download_image(handout["assignment_id"], Path("image.nii.gz"))
client.download_segmentation(handout["assignment_id"], Path("segmentation.seg.nrrd"))
# ... review in 3D Slicer ...
client.submit(handout["assignment_id"], quality_check_confirmed=True,
              segmentation_path=Path("reviewed.seg.nrrd"))
```

An upload must be a single-layer `.seg.nrrd` on the image's voxel grid, and every segment
must resolve to a BoneHub label — through its `BoneHubValue` tag, else its name, as
`bonehub_data_schema.read_segmentation` reads it. Anything else is refused with a message
naming the problem.

## Configuration

Settings are read from `<dataset-root>/.bonehub_qc/config.json` and can be overridden at
startup by an environment variable per field, named `BONEHUB_QC_<FIELD>`. They are also
editable from the admin panel. The most used ones:

| Setting | Default | Meaning |
| --- | --- | --- |
| `allowed_dataset_ids` | `null` | Restrict the server to these dataset ids; `null` means every dataset under the root |
| `eligible_label_values` | `[1]` | Label statuses that queue a subject (`1` not reviewed, `2` reviewed) |
| `include_subjects_without_segmentation` | `false` | Also queue subjects with an image but no available label, to segment from scratch |
| `mark_removed_labels_absent` | `true` | Set a label deleted by the reviewer to `0` |
| `lease_ttl_seconds` | `86400` | How long a reviewer keeps a subject |
| `max_concurrent_assignments_per_user` | `1` | Subjects one reviewer may hold at once |
| `assignment_strategy` | `sequential` | `sequential` or `random` handout order |
| `requeue_rejected` | `false` | Hand rejected subjects out again |
| `require_geometry_match` | `true` | Reject uploads whose voxel grid differs from the image |
| `keep_segmentation_backups` | `true` | Back up a segmentation before overwriting it |
| `max_upload_bytes` | `536870912` | Largest accepted segmentation upload |

See [`.env.example`](.env.example) for the Docker-facing subset and
[`config.py`](bonehub_quality_check_server/config.py) for the full list.

## Server state

Everything the server owns lives in one folder inside the dataset, so nothing is lost when
the container is recreated:

```
<dataset-root>/.bonehub_qc/
├── config.json           quality-check policy, with the schema version its statuses belong to
├── server_private_key    generated on first start
├── admin_key             generated on first start
├── users.json            reviewers and hashed API keys
├── assignments.json      open and finished assignments
├── submissions.jsonl     append-only audit trail
├── server.log            server lifecycle and administrative events
├── backups/              previous segmentations (.seg.nrrd), kept before overwriting
└── tmp/                  uploads being validated
```

Changing `BONEHUB_QC_PRIVATE_KEY` invalidates every reviewer API key already issued.

## License

See [LICENSE](LICENSE).

## Tests

The suite is plain `unittest` — no pytest, no plugins. It builds a throw-away dataset in
BoneHub data structure format under a temporary folder for every test, so it never touches
a real dataset.

```bash
conda activate bonehub-qc
python -m unittest discover -s tests            # everything
python -m unittest tests.test_submission -v     # one module
python -m unittest tests.test_submission.ConfirmedSubmissionTests.test_confirmed_labels_become_reviewed_in_subject_info
```

The suite needs `bonehub_data_schema` 0.3 with its `[io]` extra (`pip install -e .` pulls
it in); the fixtures write their masks with the schema's own functions.

| Module | What it covers |
| --- | --- |
| `test_config.py` | The policy file, its `BONEHUB_QC_*` overrides, and upgrading a file from an older schema |
| `test_auth.py` | Server private key, per-reviewer API keys, disabling and rotation |
| `test_queue.py` | Which subjects are queued, schema versions, restricting the server to specific datasets, broken dataset folders |
| `test_assignment.py` | Who gets which subject, leases, expiry, release, requeue policy |
| `test_submission.py` | Confirmed submissions marking labels reviewed (2), the `.seg.nrrd` format and its validation, rejections changing nothing, partial uploads, audit trail |
| `test_api.py` | The REST API over HTTP, as the 3D Slicer extension calls it |
| `test_admin.py` | The admin panel endpoints behind the admin key |
| `test_client.py` | `client.py` against a real uvicorn server on a real socket |
| `test_cli.py` | `bonehub-qc-server` commands |
| `test_concurrency.py` | Several reviewers hitting the server at once |
| `test_deployment.py` | Start-up from environment variables only, and the shipped docker files |

`tests/support.py` holds the dataset builder and the base test case.
