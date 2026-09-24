"""Reading an uploaded segmentation, writing it into the dataset, and describing a stored one.

The dataset keeps its masks in BoneHub's segmentation format (``.seg.nrrd``, see
``bonehub_data_schema/segmentation_file.py``): voxels hold per-file segment numbers, and the
header maps each number to a ``BoneLabelMap`` value. An upload is read by the same rules as
the schema's ``read_segmentation`` -- a segment's ``BoneHubValue`` tag, else its name -- and
is then written back through ``write_indexed_segmentation``, so what lands in the dataset is
always the canonical form, whichever client wrote the upload.

A client that only displays a mask, such as the browser review page, is sent the header's
segment table instead (:func:`read_segment_table`): which number is which label, and the
colour and bounding box the file records for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import SimpleITK as sitk

from bonehub_data_schema import SEGMENTATION_SUFFIX, BoneLabelMap, segment_number_dtype, write_indexed_segmentation

#: Segment tags the schema writes: ``BoneHubLabel:<name>|BoneHubValue:<value>|``.
LABEL_TAG = "BoneHubLabel"
VALUE_TAG = "BoneHubValue"

#: The format holds at most 65535 segments (uint16), so no larger number can be a segment.
MAX_SEGMENT_NUMBER = 65535

#: Slices handled at a time when scanning or renumbering a mask, to limit memory.
_CHUNK_SLICES = 32

#: Tolerance on the voxel-to-world matrix when an upload is compared with the image, in mm.
_GEOMETRY_TOLERANCE = 1e-3

#: Colour given to a segment whose header records none. The schema always writes one.
_FALLBACK_COLOR = (0.6, 0.6, 0.6)

_LABEL_NAMES = {label.value: label.name for label in BoneLabelMap if label.value != 0}
_LABEL_VALUES = {name: value for value, name in _LABEL_NAMES.items()}

_INTEGER_PIXEL_IDS = {
    sitk.sitkUInt8,
    sitk.sitkUInt16,
    sitk.sitkUInt32,
    sitk.sitkUInt64,
    sitk.sitkInt8,
    sitk.sitkInt16,
    sitk.sitkInt32,
    sitk.sitkInt64,
}


class SegmentationError(ValueError):
    """The upload is not a segmentation the dataset can take. The message is for the reviewer."""


@dataclass
class UploadedSegmentation:
    """A validated upload: its voxels, and the BoneLabelMap value of each number painted."""

    image: sitk.Image
    value_of_number: dict[int, int]

    @property
    def labels(self) -> dict[str, int]:
        """``{label name: BoneLabelMap value}`` for every label that has voxels."""
        return {_LABEL_NAMES[value]: value for value in sorted(set(self.value_of_number.values()))}


@dataclass
class SegmentDescription:
    """One segment as a ``.seg.nrrd`` header describes it."""

    number: int
    label: str
    value: int
    color: tuple[float, float, float]
    #: ``(i_min, i_max, j_min, j_max, k_min, k_max)`` in the file's voxel indices, or None.
    extent: tuple[int, int, int, int, int, int] | None


def read_segment_table(path: Path) -> list[SegmentDescription]:
    """The segments a ``.seg.nrrd`` describes, from its header alone, by segment number.

    Labels are read by the same rules as an upload, so a header the server would refuse
    from a client is refused here too. Raises :class:`SegmentationError`.
    """
    reader = sitk.ImageFileReader()
    reader.SetImageIO("NrrdImageIO")
    reader.SetFileName(str(path))
    try:
        reader.ReadImageInformation()
    except RuntimeError as exc:
        raise SegmentationError(f"'{Path(path).name}' could not be read as a BoneHub segmentation file.") from exc

    value_of_number = _segment_values(reader)
    described: dict[int, SegmentDescription] = {}
    i = 0
    while reader.HasMetaDataKey(f"Segment{i}_LabelValue"):
        number = int(reader.GetMetaData(f"Segment{i}_LabelValue"))
        extent = _extent(reader, i)
        if number in described:
            # Two segments of one label may share a number; together they span both boxes.
            described[number].extent = _union(described[number].extent, extent)
        else:
            value = value_of_number[number]
            described[number] = SegmentDescription(number, _LABEL_NAMES[value], value, _color(reader, i), extent)
        i += 1
    return [described[number] for number in sorted(described)]


def read_segmentation_upload(path: Path, image_path: Path | None = None) -> UploadedSegmentation:
    """Read and validate an uploaded ``.seg.nrrd``.

    With ``image_path``, the upload must sit on the image's voxel grid, and it takes the
    image's exact geometry, so a round trip through a client cannot shift the mask by a
    rounding error. Raises :class:`SegmentationError` when the upload is unusable.
    """
    reader = sitk.ImageFileReader()
    reader.SetImageIO("NrrdImageIO")
    reader.SetFileName(str(path))
    try:
        reader.ReadImageInformation()
    except RuntimeError as exc:
        raise SegmentationError(
            f"The uploaded segmentation could not be read as a BoneHub segmentation file ({SEGMENTATION_SUFFIX})."
        ) from exc

    if reader.GetDimension() != 3:
        raise SegmentationError(f"The segmentation must be a 3D mask; the upload has {reader.GetDimension()} dimensions.")
    if reader.GetNumberOfComponents() != 1:
        raise SegmentationError(
            f"The segmentation holds {reader.GetNumberOfComponents()} layers (overlapping segments). A BoneHub mask "
            "holds one segment per voxel, so it must be a single layer."
        )
    if reader.GetPixelID() not in _INTEGER_PIXEL_IDS:
        raise SegmentationError(
            "The segmentation voxels must hold integer segment numbers, "
            f"not {sitk.GetPixelIDValueAsString(reader.GetPixelID())}."
        )

    value_of_number = _segment_values(reader)
    # Checked before the voxels are read, so a wrong upload is refused without decompressing it.
    reference = _read_information(image_path) if image_path is not None else None
    if reference is not None:
        _check_geometry(reader, reference)

    try:
        image = reader.Execute()
    except RuntimeError as exc:
        raise SegmentationError("The voxels of the uploaded segmentation could not be read.") from exc

    painted = _numbers_present(sitk.GetArrayViewFromImage(image))
    undescribed = [number for number in painted if number not in value_of_number]
    if undescribed:
        raise SegmentationError(
            f"Voxels hold segment number(s) {undescribed}, which the file header does not map to a BoneHub label."
        )

    if reference is not None and reference.GetDimension() == 3:
        image.SetOrigin(reference.GetOrigin())
        image.SetSpacing(reference.GetSpacing())
        image.SetDirection(reference.GetDirection())
    return UploadedSegmentation(image=image, value_of_number={n: value_of_number[n] for n in painted})


def write_segmentation(upload: UploadedSegmentation, output_path: Path) -> list[str]:
    """Write an upload in canonical form: numbered 1, 2, ... in ascending label value.

    Segments of the same label are merged into one. Returns the names of the labels written.
    """
    values = sorted(set(upload.value_of_number.values()))
    number_of = {value: number for number, value in enumerate(values, start=1)}
    dtype = segment_number_dtype(len(values))

    lookup = np.zeros(max(upload.value_of_number, default=0) + 1, dtype=dtype)
    for number, value in upload.value_of_number.items():
        lookup[number] = number_of[value]

    source = sitk.GetArrayViewFromImage(upload.image)
    numbers = np.empty(source.shape, dtype=dtype)
    for start in range(0, source.shape[0], _CHUNK_SLICES):
        chunk = slice(start, start + _CHUNK_SLICES)
        numbers[chunk] = lookup[source[chunk]]
    return write_indexed_segmentation(numbers, values, upload.image, Path(output_path))


# --------------------------------------------------------------------- helpers
def _segment_values(reader: sitk.ImageFileReader) -> dict[int, int]:
    """``{segment number: BoneLabelMap value}`` from the header, as ``read_segmentation`` reads it.

    Stricter than the schema's reader in one way: a segment whose name is one BoneHub label
    while its tags say another is refused, since there is no telling which one was meant.
    """
    value_of: dict[int, int] = {}
    named: dict[int, str] = {}
    i = 0
    while reader.HasMetaDataKey(f"Segment{i}_LabelValue"):
        name = reader.GetMetaData(f"Segment{i}_Name") if reader.HasMetaDataKey(f"Segment{i}_Name") else ""
        try:
            number = int(reader.GetMetaData(f"Segment{i}_LabelValue"))
        except ValueError as exc:
            raise SegmentationError(f"Segment '{name}' has no valid segment number in the file header.") from exc
        if not 1 <= number <= MAX_SEGMENT_NUMBER:
            raise SegmentationError(f"Segment '{name}' has segment number {number}; numbers run from 1 to 65535.")

        value = _value_of_segment(name, _tags(reader, i))
        if number in value_of and value_of[number] != value:
            raise SegmentationError(f"Segments '{named[number]}' and '{name}' both use segment number {number}.")
        value_of[number] = value
        named[number] = name
        i += 1
    return value_of


def _value_of_segment(name: str, tags: dict[str, str]) -> int:
    by_name = _LABEL_VALUES.get(name)
    if VALUE_TAG not in tags:
        if by_name is None:
            raise SegmentationError(f"Segment '{name}' is not a BoneHub label. See 'bonehub_data_schema/labelmap.py'.")
        return by_name

    try:
        value = int(tags[VALUE_TAG])
    except ValueError:
        value = None
    if value not in _LABEL_NAMES:
        raise SegmentationError(f"Segment '{name}' is tagged {VALUE_TAG}:{tags[VALUE_TAG]}, which is not a BoneHub label.")
    if by_name is not None and by_name != value:
        raise SegmentationError(
            f"Segment '{name}' is tagged as BoneHub label {_LABEL_NAMES[value]}. Rename the segment or remove the tag."
        )
    if LABEL_TAG in tags and tags[LABEL_TAG] != _LABEL_NAMES[value]:
        raise SegmentationError(
            f"Segment '{name}' carries two BoneHub labels in its tags: {tags[LABEL_TAG]} and {_LABEL_NAMES[value]}."
        )
    return value


def _tags(reader: sitk.ImageFileReader, i: int) -> dict[str, str]:
    key = f"Segment{i}_Tags"
    if not reader.HasMetaDataKey(key):
        return {}
    return dict(tag.split(":", 1) for tag in reader.GetMetaData(key).split("|") if ":" in tag)


def _color(reader: sitk.ImageFileReader, i: int) -> tuple[float, float, float]:
    key = f"Segment{i}_Color"
    try:
        red, green, blue = (float(part) for part in reader.GetMetaData(key).split())
    except (RuntimeError, ValueError):  # the key is missing, or does not hold three numbers
        return _FALLBACK_COLOR
    return tuple(min(max(channel, 0.0), 1.0) for channel in (red, green, blue))


def _extent(reader: sitk.ImageFileReader, i: int) -> tuple[int, int, int, int, int, int] | None:
    """The segment's bounding box from ``Segment<i>_Extent``; None for a missing or empty one."""
    key = f"Segment{i}_Extent"
    if not reader.HasMetaDataKey(key):
        return None
    try:
        extent = tuple(int(part) for part in reader.GetMetaData(key).split())
    except ValueError:
        return None
    if len(extent) != 6 or any(extent[axis] > extent[axis + 1] for axis in (0, 2, 4)):
        return None
    return extent


def _union(first, second):
    if first is None or second is None:
        return first or second
    return tuple(min(a, b) if axis % 2 == 0 else max(a, b) for axis, (a, b) in enumerate(zip(first, second)))


def _read_information(path: Path) -> sitk.ImageFileReader:
    reader = sitk.ImageFileReader()
    reader.SetFileName(str(path))
    reader.ReadImageInformation()
    return reader


def _affine(reader: sitk.ImageFileReader) -> np.ndarray:
    """Voxel-to-world matrix: the direction's columns scaled by the spacing, then the origin."""
    affine = np.eye(4)
    direction = np.asarray(reader.GetDirection(), dtype=float).reshape(3, 3)
    affine[:3, :3] = direction * np.asarray(reader.GetSpacing(), dtype=float)
    affine[:3, 3] = reader.GetOrigin()
    return affine


def check_stored_geometry(segmentation_path: Path, image_path: Path) -> None:
    """Check from the headers alone that a stored segmentation sits on its image's voxel grid.

    Raises :class:`SegmentationError` saying how they differ.
    """
    try:
        segmentation, image = _read_information(segmentation_path), _read_information(image_path)
    except RuntimeError as exc:
        raise SegmentationError(f"The headers of '{Path(segmentation_path).name}' or its image could not be read.") from exc
    _check_geometry(segmentation, image)


def _check_geometry(segmentation: sitk.ImageFileReader, image: sitk.ImageFileReader) -> None:
    if tuple(segmentation.GetSize()) != tuple(image.GetSize()[:3]):
        raise SegmentationError(
            f"Segmentation size {tuple(segmentation.GetSize())} does not match the image {tuple(image.GetSize()[:3])}."
        )
    if image.GetDimension() == 3:
        mask_affine, image_affine = _affine(segmentation), _affine(image)
        if not np.allclose(mask_affine, image_affine, atol=_GEOMETRY_TOLERANCE):
            difference = float(np.abs(mask_affine - image_affine).max())
            raise SegmentationError(
                "The segmentation's spacing, origin or orientation (its affine) does not match the image "
                f"(they differ by up to {difference:.2g} mm)."
            )


def _numbers_present(array: np.ndarray) -> list[int]:
    """The non-zero segment numbers that have voxels, read a slab at a time."""
    if array.dtype.kind == "i" and array.size and array.min() < 0:
        raise SegmentationError("The segmentation holds negative segment numbers.")
    found: set[int] = set()
    for start in range(0, array.shape[0], _CHUNK_SLICES):
        slab = array[start : start + _CHUNK_SLICES]
        if array.dtype.itemsize <= 2:
            found.update(int(n) for n in np.flatnonzero(np.bincount(slab.ravel())))
        else:
            found.update(int(n) for n in np.unique(slab))
    found.discard(0)
    return sorted(found)
