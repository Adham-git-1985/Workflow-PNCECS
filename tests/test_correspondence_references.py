import unittest
from unittest.mock import patch

from utils.corr_refs import correspondence_reference_label
from utils.corr_stamps import CorrStampOptions, _shape_stamp_text, build_stamp_image


class CorrespondenceReferenceTests(unittest.TestCase):
    def test_new_reference_does_not_repeat_the_correspondence_type(self):
        self.assertEqual(
            correspondence_reference_label(
                "IN", "وارد-1608-2026-000001", include_number_word=True
            ),
            "وارد-1608-2026-000001",
        )
        self.assertEqual(
            correspondence_reference_label(
                "OUT", "صادر-1608-2026-000001", include_number_word=True
            ),
            "صادر-1608-2026-000001",
        )

    def test_legacy_reference_still_gets_a_clear_type_label(self):
        self.assertEqual(
            correspondence_reference_label("IN", "IN-2026-0001"),
            "وارد IN-2026-0001",
        )
        self.assertEqual(
            correspondence_reference_label(
                "OUT", "OUT-2026-0001", include_number_word=True
            ),
            "صادر رقم OUT-2026-0001",
        )

    def test_incoming_reference_digits_keep_their_logical_order_in_rtl_stamp(self):
        displayed = _shape_stamp_text("وارد-0208-2026-000011")

        self.assertIn("0208-2026-000011", displayed)
        self.assertNotIn("000011-2026-0208", displayed)
        self.assertNotIn("\u200f", displayed)

    @patch("utils.corr_stamps.features.check", return_value=True)
    def test_native_rtl_renderer_receives_logical_reference(self, _features_check):
        draw = unittest.mock.Mock()
        draw.textbbox.return_value = (0, 0, 100, 40)
        reference = "وارد-0208-2026-000011"

        from utils.corr_stamps import _draw_centered

        _draw_centered(draw, reference, unittest.mock.Mock(), (0, 0, 0), 0, 200, 0)

        self.assertEqual(draw.text.call_args.args[1], reference)
        self.assertEqual(draw.text.call_args.kwargs["direction"], "rtl")

    @patch("utils.corr_stamps._load_eagle", return_value=None)
    @patch("utils.corr_stamps._draw_centered")
    def test_file_stamp_uses_each_type_only_once(self, draw_centered, _load_eagle):
        for kind, reference in (
            ("IN", "وارد-1608-2026-000001"),
            ("OUT", "صادر-1608-2026-000001"),
        ):
            with self.subTest(kind=kind):
                draw_centered.reset_mock()
                build_stamp_image(CorrStampOptions(True, kind, reference, "2026-08-16"))
                self.assertEqual(draw_centered.call_args_list[-1].args[1], reference)


if __name__ == "__main__":
    unittest.main()
