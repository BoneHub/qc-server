"""
BoneHub Dataset Quality Check - server.

Points at a folder that is already in BoneHub data structure format, hands subjects out
to authenticated reviewers -- in 3D Slicer, or on the browser review page at ``/review`` --
and receives their verdicts, with reviewed segmentations (``.seg.nrrd``), back. When a
reviewer confirms the quality, the reviewed labels in ``Subject_info_XXX.json`` are set to
status ``2`` ("available, reviewed and corrected").
"""

from .config import QCServerConfig
from .models import Assignment, AssignmentState, SubmissionResult, User

__all__ = [
    "QCServerConfig",
    "Assignment",
    "AssignmentState",
    "SubmissionResult",
    "User",
]

__version__ = "0.2.0"
__author__ = "Hamid Alavi"
