"""
BoneHub Dataset Quality Check - server.

Points at a folder that is already in BoneHub data structure format, hands subjects out
to authenticated users -- editors, who correct them in 3D Slicer, and reviewers, who check
them on the browser review page at ``/review`` -- and receives their verdicts, with
corrected segmentations (``.seg.nrrd``), back. When a subject is confirmed, the labels
vouched for in ``Subject_info_XXX.json`` are set to status ``2`` ("available, reviewed and
corrected").
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

__version__ = "0.3.0"
__author__ = "Hamid Alavi"
