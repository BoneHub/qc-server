"""The browser review page: what the server serves for it, and what it tells the page.

The page itself runs in the reviewer's browser and is checked by hand (see the README); these
tests hold the server's side of it. That covers the files the page is made of and what gets
installed with the package, the vendored NiiVue build, and the segment table each handout
carries so the page can name and colour the labels.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import re
import unittest
import urllib.parse
from pathlib import Path

import numpy as np
import SimpleITK as sitk

from bonehub_quality_check_server.config import QCServerConfig
from bonehub_quality_check_server.models import REJECT_REASONS, SubmissionRequest
from bonehub_quality_check_server.review import STATIC_DIR
from bonehub_quality_check_server.segmentation import SegmentationError, read_segment_table

from tests.support import LABEL_VALUE, QCTestCase, segment_header, write_mask, write_raw_mask
from tests.test_api import ApiTestCase

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = STATIC_DIR.parent


def niivue_import() -> str:
    """The file review.js imports NiiVue from, relative to static/."""
    script = (STATIC_DIR / "review.js").read_text(encoding="utf-8")
    match = re.search(r'from\s+"\./(vendor/niivue-[^"]+\.js)"', script)
    if match is None:
        raise AssertionError("review.js does not import NiiVue from static/vendor/.")
    return match.group(1)


class ServedFilesTests(ApiTestCase):
    def test_the_page_is_served_with_a_content_security_policy(self):
        response = self.client.get("/review")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        policy = response.headers["content-security-policy"]
        self.assertIn("script-src 'self'", policy, "only the server's own scripts may run next to the key")
        self.assertIn("connect-src 'self'", policy)
        self.assertIn("frame-ancestors 'none'", policy)

    def test_the_page_needs_no_key_but_everything_it_fetches_does(self):
        self.assertEqual(self.client.get("/review").status_code, 200)
        self.assertEqual(self.client.post("/api/v1/subjects/next").status_code, 401)

    def test_the_script_is_served_as_javascript(self):
        """A module script served as text/plain is refused by the browser."""
        response = self.client.get("/static/review.js")
        self.assertEqual(response.status_code, 200)
        self.assertIn("javascript", response.headers["content-type"])

    def test_the_niivue_build_the_script_imports_is_served(self):
        response = self.client.get(f"/static/{niivue_import()}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("javascript", response.headers["content-type"])

    def test_the_admin_panel_points_reviewers_to_the_page(self):
        self.assertIn('href="/review"', self.client.get("/admin").text)


class VendoredNiivueTests(unittest.TestCase):
    def test_the_build_exists_with_its_license(self):
        self.assertTrue((STATIC_DIR / niivue_import()).is_file())
        self.assertIn("BSD 2-Clause", (STATIC_DIR / "vendor" / "LICENSE-niivue.txt").read_text(encoding="utf-8"))

    def test_the_build_imports_nothing(self):
        """The page is served from this server alone, so NiiVue must be one self-contained file."""
        code = (STATIC_DIR / niivue_import()).read_text(encoding="utf-8")
        self.assertIsNone(re.search(r'(?:^|[;}\n])\s*import\s*(?:[\w*{][^;]*?\bfrom\s*)?["\']', code))
        self.assertRegex(code[-5000:], r"export\s*\{[^}]*\bNiivue\b")

    def test_only_one_build_is_vendored(self):
        builds = sorted(path.name for path in (STATIC_DIR / "vendor").glob("niivue-*.js"))
        self.assertEqual(builds, [Path(niivue_import()).name], "delete the old build after an update")

    def test_every_static_file_is_installed_with_the_package(self):
        """setuptools installs only what package-data names; the Docker image is built with pip."""
        pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        line = re.search(r"^bonehub_quality_check_server\s*=\s*\[(.*)\]", pyproject, re.M)
        self.assertIsNotNone(line, "no package-data entry for bonehub_quality_check_server")
        patterns = re.findall(r'"([^"]+)"', line.group(1))
        missing = [
            path.relative_to(PACKAGE_DIR).as_posix()
            for path in STATIC_DIR.rglob("*")
            if path.is_file()
            and not any(fnmatch.fnmatch(path.relative_to(PACKAGE_DIR).as_posix(), pattern) for pattern in patterns)
        ]
        self.assertEqual(missing, [])


class UpdateScriptTests(unittest.TestCase):
    """tools/update_niivue.py, without the network: how it unpacks NiiVue's bundle."""

    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location("update_niivue", PROJECT_ROOT / "tools" / "update_niivue.py")
        cls.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.tool)

    def bundle(self, code: str) -> str:
        return f'export const esm = "{urllib.parse.quote(code)}";'

    def test_a_self_contained_bundle_is_decoded(self):
        code = "var a=1;class Niivue{};export{Niivue,a};"
        self.assertEqual(self.tool.decode_bundle(self.bundle(code)), code)

    def test_a_bundle_that_imports_is_refused(self):
        with self.assertRaises(SystemExit):
            self.tool.decode_bundle(self.bundle('import{mat4}from"gl-matrix";class Niivue{};export{Niivue};'))

    def test_an_unexpected_layout_is_refused(self):
        with self.assertRaises(SystemExit):
            self.tool.decode_bundle("export default {}")


class PageContractTests(unittest.TestCase):
    """The pages cannot import the server's names, so they must spell them as the server does."""

    def read(self, name: str) -> str:
        return (STATIC_DIR / name).read_text(encoding="utf-8")

    def test_the_review_page_knows_every_reason_to_reject_a_label(self):
        match = re.search(r"const REASON_TEXT = \{([^}]*)\}", self.read("review.js"))
        self.assertIsNotNone(match, "review.js names the reasons in REASON_TEXT")
        self.assertEqual(set(re.findall(r"(\w+):", match.group(1))), set(REJECT_REASONS))

    def test_the_review_page_sends_the_verdict_the_server_reads(self):
        script = self.read("review.js")
        sent = {"quality_check_confirmed", "use_stored_segmentation", "confirmed_labels", "rejected_labels", "missing_labels", "comment"}
        self.assertEqual(sent, set(SubmissionRequest.model_fields))
        for field in sent:
            self.assertIn(field, script)

    def test_the_admin_panel_saves_only_settings_the_server_has(self):
        page = self.read("admin.html")
        match = re.search(r'api\("PUT", "/admin/api/config", \{(.*?)\}\)', page, re.S)
        self.assertIsNotNone(match)
        saved = set(re.findall(r"^\s*(\w+):", match.group(1), re.M))
        self.assertIn("edits_need_review", saved)
        self.assertEqual(saved - set(QCServerConfig.model_fields), set())

    def test_the_admin_panel_approves_through_the_case_endpoints(self):
        page = self.read("admin.html")
        for endpoint in ('"/admin/api/cases/approve"', '"/approve"', '"/return"', '"/close"', '"/segmentation"'):
            self.assertIn(endpoint, page)


class SegmentTableTests(QCTestCase):
    """What the review page is told about a stored segmentation: read from its header alone."""

    def test_each_segment_is_described_by_its_header(self):
        path = write_mask(self.tmp_path / "mask.seg.nrrd", ["FEMUR_RIGHT", "FEMUR_LEFT"])
        segments = read_segment_table(path)

        self.assertEqual([s.label for s in segments], ["FEMUR_LEFT", "FEMUR_RIGHT"])
        self.assertEqual([s.number for s in segments], [1, 2])
        self.assertEqual(segments[0].value, LABEL_VALUE["FEMUR_LEFT"])

        reader = sitk.ImageFileReader()
        reader.SetFileName(str(path))
        reader.ReadImageInformation()
        header_color = tuple(float(c) for c in reader.GetMetaData("Segment0_Color").split())
        np.testing.assert_allclose(segments[0].color, header_color)

    def test_the_extent_is_the_bounding_box_in_the_files_voxel_indices(self):
        # The fixture paints label n into slice z = n, x and y from 0 to 1.
        segments = read_segment_table(write_mask(self.tmp_path / "mask.seg.nrrd", ["FEMUR_LEFT", "FEMUR_RIGHT"]))
        self.assertEqual(segments[0].extent, (0, 1, 0, 1, 0, 0))
        self.assertEqual(segments[1].extent, (0, 1, 0, 1, 1, 1))

    def test_segments_of_one_label_sharing_a_number_are_described_once(self):
        header = segment_header((1, "FEMUR_LEFT", None), (1, "FEMUR_LEFT", None))
        header.update({"Segment0_Extent": "0 1 0 1 0 0", "Segment1_Extent": "2 3 2 3 1 1"})
        numbers = np.zeros((6, 6, 6), dtype=np.uint8)
        numbers[0, 0:2, 0:2] = 1
        numbers[1, 2:4, 2:4] = 1
        segments = read_segment_table(write_raw_mask(self.tmp_path / "mask.seg.nrrd", numbers, header))
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].extent, (0, 3, 0, 3, 0, 1))

    def test_a_segment_without_colour_or_extent_still_gets_a_row(self):
        numbers = np.zeros((6, 6, 6), dtype=np.uint8)
        numbers[0, 0:2, 0:2] = 1
        path = write_raw_mask(self.tmp_path / "mask.seg.nrrd", numbers, segment_header((1, "SACRUM", None)))
        segment = read_segment_table(path)[0]
        self.assertEqual(segment.label, "SACRUM")
        self.assertIsNone(segment.extent)
        self.assertTrue(all(0.0 <= c <= 1.0 for c in segment.color))

    def test_a_segment_that_is_no_bonehub_label_is_refused(self):
        numbers = np.zeros((6, 6, 6), dtype=np.uint8)
        numbers[0, 0:2, 0:2] = 1
        path = write_raw_mask(self.tmp_path / "mask.seg.nrrd", numbers, segment_header((1, "NOT_A_BONE", None)))
        with self.assertRaises(SegmentationError):
            read_segment_table(path)


class HandoutSegmentTests(ApiTestCase):
    def test_the_handout_carries_the_segment_table(self):
        handout = self.next_subject(self.alice_key)
        self.assertEqual([s["label"] for s in handout["segments"]], ["FEMUR_LEFT", "FEMUR_RIGHT"])
        first = handout["segments"][0]
        self.assertEqual(set(first), {"number", "label", "value", "color", "extent"})
        self.assertEqual(first["value"], LABEL_VALUE["FEMUR_LEFT"])
        self.assertEqual(len(first["color"]), 3)
        self.assertEqual(first["extent"], [0, 1, 0, 1, 0, 0])

    def test_a_header_the_server_cannot_read_leaves_the_handout_working(self):
        """The 3D Slicer extension does not need the table, so it must not lose its handout."""
        numbers = np.zeros((6, 6, 6), dtype=np.uint8)
        numbers[0, 0:2, 0:2] = 1
        write_raw_mask(self.builder.segmentation_file(1, 1), numbers, segment_header((1, "NOT_A_BONE", None)))
        handout = self.next_subject(self.alice_key)
        self.assertTrue(handout["has_segmentation"])
        self.assertEqual(handout["segments"], [])
        self.assertIn("segment table", (self.state_dir / "server.log").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
