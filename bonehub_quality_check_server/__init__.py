"""
BoneHub Dataset Quality Check - server.

Points at a folder that is already in BoneHub data structure format, hands subjects out
to authenticated 3D Slicer reviewers, and receives reviewed segmentations back. When a
reviewer confirms the quality, the corresponding label values in ``Subject_info_XXX.json``
are promoted to ``3`` ("available, ..., passed quality check").
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

__version__ = "0.1.0"
__author__ = "Hamid Alavi"
