"""A subject's way through the quality check, from its first review to the administrator's approval.

Each subject a user gives a verdict on gets a *case* on this server (``models.Case``), kept in
the server's state folder together with any corrected segmentation. The dataset is left alone
until the administrator approves the case: only then are its accepted labels set to status 2,
and a corrected segmentation written in.

A case is at one *stage*::

    review     a label waits for a reviewer's verdict          -> handed to reviewers
    edit       a reviewer rejected a label                     -> handed to editors
    approval   every label is accepted, kept or removed        -> waits for the administrator
    escalated  an editor could not correct it                  -> waits for the administrator
    applied    approved and written into the dataset           (finished)
    closed     closed by the administrator, nothing written    (finished, until reopened)

and each of its labels in one *state*, painted in the segmentation under review or not::

    pending    waits for a reviewer: the dataset has it as not reviewed (a status the server
               queues on), or an editor changed it. Not painted, it is an editor's removal
               waiting for a reviewer to agree.
    accepted   a reviewer accepted it -- or its editor, when edits need no review
    rejected   a reviewer rejected it: it needs correcting ('quality'), should not be there
               ('absent'), or, not painted, should be there and is not ('missing')
    removed    not in the segmentation, and nobody need look at it again; set to 0 on approval
    kept       not under review -- the dataset has it as reviewed already -- and unchanged

A reviewer's verdict accepts or rejects each label under review, and may reject any other
too, or report a bone the segmentation lacks as missing. An editor's upload replaces the
segmentation. A label whose voxels it changed, one it added, and one a reviewer had rejected
are *edited*: they go back to ``pending`` -- or, when edits need no review, to ``accepted``
if the editor vouches for them. A label the upload left alone keeps its state, so a
correction that spills into an accepted neighbour takes the neighbour's acceptance away, and
one that does not leaves it. A label the upload takes away is removed; unless a reviewer
asked for that ('absent'), a reviewer must agree first, when edits need review. A label
nobody has reviewed stays ``pending`` until a reviewer has: an editor's word is not a review.

Everything here works on the case alone; the store reads and writes the files.
"""

from __future__ import annotations

from collections.abc import Iterable

from .config import STATUS_NOT_AVAILABLE, STATUS_NOT_REVIEWED, STATUS_REVIEWED
from .models import EDITOR, REJECT_REASONS, REVIEWER, Case, CaseEvent, CaseLabel, CaseRequest, FileFingerprint

REVIEW, EDIT, APPROVAL, ESCALATED, APPLIED, CLOSED = "review", "edit", "approval", "escalated", "applied", "closed"

#: Stages in which a case is still in progress on this server.
OPEN_STAGES: tuple[str, ...] = (REVIEW, EDIT, APPROVAL, ESCALATED)

#: Stages in which a case is finished; a closed one can be reopened.
FINISHED_STAGES: tuple[str, ...] = (APPLIED, CLOSED)

PENDING, ACCEPTED, REJECTED, REMOVED, KEPT = "pending", "accepted", "rejected", "removed", "kept"

QUALITY, ABSENT, MISSING = "quality", "absent", "missing"

#: The stage at which each role is handed a subject.
STAGE_OF_ROLE: dict[str, str] = {REVIEWER: REVIEW, EDITOR: EDIT}

#: Who acts for the administrator in a case's history.
ADMIN = "admin"


# ----------------------------------------------------------------------- queries
def labels_in(case: Case, *states: str) -> list[str]:
    """The case's labels in any of these states, by name."""
    return sorted(name for name, label in case.labels.items() if label.state in states)


def painted_labels(case: Case) -> list[str]:
    """The labels in the case's segmentation."""
    return sorted(name for name, label in case.labels.items() if label.painted)


def derived_stage(case: Case) -> str:
    """The stage a case's labels and requests call for.

    An editor comes first: any label rejected, or any request open, needs a correction before
    the rest is worth reviewing again. A case with nothing painted at all needs an editor too.
    """
    states = [label.state for label in case.labels.values()]
    if REJECTED in states or case.requests or not any(label.painted for label in case.labels.values()):
        return EDIT
    if PENDING in states:
        return REVIEW
    return APPROVAL


# ------------------------------------------------------------------ transitions
def start_case(
    dataset_id: int,
    subject_id: int,
    subject_key: str,
    statuses: dict[str, int],
    painted: Iterable[str],
    eligible: Iterable[int],
    base: FileFingerprint | None,
    now: str,
) -> Case:
    """The case a subject begins with, from its Subject_info ``statuses`` and the labels its
    segmentation ``painted``.

    A painted label is under review when its status is one the server queues on
    (``eligible``), and also when the dataset lists it as not available or not at all, since
    then nobody has looked at it. Any other painted label is kept as it is. A label Subject_info
    lists as available that the segmentation does not paint is removed.
    """
    eligible = set(eligible)
    labels: dict[str, CaseLabel] = {}
    for name in painted:
        status = statuses.get(name, STATUS_NOT_AVAILABLE)
        under_review = status == STATUS_NOT_AVAILABLE or status in eligible
        labels[name] = CaseLabel(state=PENDING if under_review else KEPT)
    for name, status in statuses.items():
        if name not in labels and status != STATUS_NOT_AVAILABLE:
            labels[name] = CaseLabel(state=REMOVED, painted=False)
    case = Case(
        subject_key=subject_key,
        dataset_id=dataset_id,
        subject_id=subject_id,
        stage=REVIEW,
        labels=dict(sorted(labels.items())),
        base=base,
        created_at=now,
        updated_at=now,
    )
    case.stage = derived_stage(case)
    return case


def apply_review(
    case: Case,
    reviewer: str,
    accepted: Iterable[str],
    rejected: dict[str, str],
    comment: str | None,
    assignment_id: str | None,
    now: str,
    request_edit: bool = False,
) -> dict:
    """A reviewer's verdict. The caller has checked the names and reasons against the case.

    An accepted label in the segmentation is accepted; accepting one that is not agrees to its
    removal. A rejected label goes to the editors, and so does a bone reported ``missing``,
    which the case may not have known. ``request_edit`` sends the subject to the editors even
    when no label is rejected: a rejection of the subject as a whole with nothing under review.
    """
    accepted = sorted(set(accepted))
    rejected = dict(sorted(rejected.items()))
    for name in accepted:
        label = case.labels[name]
        state = ACCEPTED if label.painted else REMOVED
        case.labels[name] = CaseLabel(state=state, painted=label.painted, by=reviewer, at=now, edited_by=label.edited_by)
    for name, reason in rejected.items():
        if reason not in REJECT_REASONS:
            raise ValueError(f"'{reason}' is not a reason to reject a label; give one of {sorted(REJECT_REASONS)}.")
        label = case.labels.get(name)
        case.labels[name] = CaseLabel(
            state=REJECTED,
            painted=label is not None and label.painted,
            reason=reason,
            by=reviewer,
            at=now,
            edited_by=label.edited_by if label is not None else None,
        )
    case.labels = dict(sorted(case.labels.items()))
    if request_edit and not rejected:
        case.requests.append(CaseRequest(by=reviewer, role=REVIEWER, at=now, comment=comment or None))
    details = {
        "accepted": accepted,
        "rejected": rejected,
        "missing": sorted(name for name, reason in rejected.items() if reason == MISSING),
    }
    _step(case, reviewer, REVIEWER, "review", derived_stage(case), comment, assignment_id, now, details)
    return details


def apply_edit(
    case: Case,
    editor: str,
    present: Iterable[str],
    changed: set[str] | None,
    vouched: Iterable[str],
    edits_need_review: bool,
    comment: str | None,
    assignment_id: str | None,
    now: str,
) -> dict:
    """An editor's corrected segmentation, which holds the labels ``present``.

    ``changed`` are the labels whose voxels the upload changed, or None when that could not
    be told, and every label counts as changed. ``vouched`` are the labels the editor vouches
    for, which counts only for labels they edited, and only when edits need no review.
    Requests are resolved: the editor has seen them, whatever they did about them.
    """
    present = set(present)
    vouched = set(vouched)
    edited, accepted, removed = [], [], []
    for name in sorted(present):
        before = case.labels.get(name)
        touched = (
            changed is None or name in changed or before is None or not before.painted or before.state == REJECTED
        )
        if not touched:
            continue
        edited.append(name)
        if not edits_need_review and name in vouched:
            case.labels[name] = CaseLabel(state=ACCEPTED, by=editor, at=now, edited_by=editor)
            accepted.append(name)
        else:
            case.labels[name] = CaseLabel(state=PENDING, at=now, edited_by=editor)
    for name, before in sorted(case.labels.items()):
        if name in present:
            continue
        if before.painted:
            # Taken away. What a reviewer asked for is done; anything else a reviewer must see.
            removed.append(name)
            asked = before.state == REJECTED and before.reason == ABSENT
            state = REMOVED if asked or not edits_need_review else PENDING
            case.labels[name] = CaseLabel(state=state, painted=False, by=editor if state == REMOVED else None,
                                          at=now, edited_by=editor)
        elif before.state == REJECTED:
            # Reported missing, and not added: the editor disagreed, and a reviewer must see that.
            state = PENDING if edits_need_review else REMOVED
            case.labels[name] = CaseLabel(state=state, painted=False, by=editor if state == REMOVED else None,
                                          at=now, edited_by=editor)
    case.labels = dict(sorted(case.labels.items()))
    resolved = [request.model_dump() for request in case.requests]
    case.requests = []
    case.staged = True
    case.edited_by = editor
    details = {
        "edited": edited,
        "accepted": accepted,
        "removed": removed,
        "pending": labels_in(case, PENDING),
        "resolved_requests": resolved,
    }
    _step(case, editor, EDITOR, "edit", derived_stage(case), comment, assignment_id, now, details)
    return details


def escalate(case: Case, editor: str, comment: str | None, assignment_id: str | None, now: str) -> dict:
    """An editor could not correct the subject: the administrator decides what becomes of it."""
    _step(case, editor, EDITOR, "escalate", ESCALATED, comment, assignment_id, now, {})
    return {}


def return_to_review(case: Case, comment: str | None, now: str, by: str = ADMIN) -> dict:
    """Send every verdict back to the reviewers: the labels someone accepted, rejected or
    removed wait for a review again, and open requests are dropped. Reopens a closed case."""
    returned = sorted(
        name
        for name, label in case.labels.items()
        if label.state in (ACCEPTED, REJECTED) or (label.state == REMOVED and label.by is not None)
    )
    for name in returned:
        label = case.labels[name]
        case.labels[name] = CaseLabel(state=PENDING, painted=label.painted, at=now, edited_by=label.edited_by)
    case.requests = []
    details = {"to": REVIEW, "labels": returned}
    _step(case, by, ADMIN, "return", derived_stage(case), comment, None, now, details)
    return details


def return_to_edit(case: Case, comment: str | None, now: str, by: str = ADMIN) -> dict:
    """Send the subject to the editors, with the administrator's word. Reopens a closed case."""
    case.requests.append(CaseRequest(by=by, role=ADMIN, at=now, comment=comment or None))
    details = {"to": EDIT}
    _step(case, by, ADMIN, "return", derived_stage(case), comment, None, now, details)
    return details


def close(case: Case, comment: str | None, now: str, by: str = ADMIN) -> dict:
    """Finish the case without writing anything into the dataset."""
    _step(case, by, ADMIN, "close", CLOSED, comment, None, now, {})
    return {}


def mark_applied(case: Case, updated_labels: dict[str, int], backup_path: str | None, now: str, by: str = ADMIN) -> dict:
    """The case was approved and written into the dataset."""
    details = {
        "updated_labels": dict(sorted(updated_labels.items())),
        "segmentation_written": case.staged,
        "backup_path": backup_path,
    }
    case.applied_at = now
    case.backup_path = backup_path
    case.staged = False
    _step(case, by, ADMIN, "approve", APPLIED, None, None, now, details)
    return details


# --------------------------------------------------------------------- approval
def approval_statuses(case: Case, statuses: dict[str, int], mark_removed_absent: bool) -> dict[str, int]:
    """The Subject_info statuses an approved case sets, by label.

    An accepted label becomes reviewed (2). One no longer painted becomes not available (0)
    when the policy says so, if it was available. A label in the segmentation that the dataset
    lists as not available is recorded as available but not reviewed (1): the file is the truth
    about what is painted.
    """
    updates: dict[str, int] = {}
    for name, label in case.labels.items():
        current = statuses.get(name, STATUS_NOT_AVAILABLE)
        if not label.painted:
            if mark_removed_absent and current != STATUS_NOT_AVAILABLE:
                updates[name] = STATUS_NOT_AVAILABLE
        elif label.state == ACCEPTED:
            updates[name] = STATUS_REVIEWED
        elif current == STATUS_NOT_AVAILABLE:
            updates[name] = STATUS_NOT_REVIEWED
    return dict(sorted(updates.items()))


# ---------------------------------------------------------------------- helpers
def _step(
    case: Case,
    by: str,
    role: str,
    action: str,
    stage: str,
    comment: str | None,
    assignment_id: str | None,
    now: str,
    details: dict,
) -> None:
    """Move the case to ``stage`` and record the step in its history."""
    case.stage = stage
    case.revision += 1
    case.updated_at = now
    case.events.append(
        CaseEvent(
            at=now,
            by=by,
            role=role,
            action=action,
            stage=stage,
            comment=comment or None,
            assignment_id=assignment_id,
            details=details,
        )
    )
