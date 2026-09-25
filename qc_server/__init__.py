"""
BoneHub Dataset Quality Check - server.

Points at a folder that is already in BoneHub data structure format, hands subjects out
to authenticated users -- reviewers, who accept or reject each label on the browser review
page at ``/review``, and editors, who correct the rejected ones in 3D Slicer -- and keeps
their verdicts, with corrected segmentations (``.seg.nrrd``), in its own state folder. When
the administrator approves a subject, its accepted labels are set to status ``2``
("available, reviewed and corrected") in ``Subject_info_XXX.json``, and a corrected
segmentation replaces the dataset's.
"""

from .config import QCServerConfig
from .models import Assignment, AssignmentState, Role, SubmissionResult, User

__all__ = [
    "QCServerConfig",
    "Assignment",
    "AssignmentState",
    "Role",
    "SubmissionResult",
    "User",
]

__version__ = "0.4.0"
__author__ = "Hamid Alavi"
