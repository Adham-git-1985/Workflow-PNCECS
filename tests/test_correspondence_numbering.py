import unittest

from flask import Flask

from extensions import db
from models import CorrCounter, InboundMail, OutboundMail
from portal.routes import _corr_format_ref, _corr_next_ref, _corr_ref_serial


class CorrespondenceNumberingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(cls.app)
        cls.context = cls.app.app_context()
        cls.context.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        cls.context.pop()

    def setUp(self):
        db.session.query(CorrCounter).delete()
        db.session.query(InboundMail).delete()
        db.session.query(OutboundMail).delete()
        db.session.commit()

    def test_reference_format_is_explicit_and_readable(self):
        self.assertEqual(_corr_format_ref("IN", "2026-08-16", 1), "وارد-16082026-000001")
        self.assertEqual(_corr_format_ref("OUT", "2026-12-03", 42), "صادر-03122026-000042")

    def test_serial_reader_supports_new_and_legacy_references(self):
        self.assertEqual(_corr_ref_serial("وارد-2026-000117", "IN", 2026), 117)
        self.assertEqual(_corr_ref_serial("وارد-16082026-000118", "IN", 2026), 118)
        self.assertEqual(_corr_ref_serial("IN-2026-0018", "IN", 2026), 18)
        self.assertEqual(_corr_ref_serial("صادر/16082026/000019", "OUT", 2026), 19)
        self.assertEqual(_corr_ref_serial("manual-reference", "OUT", 2026), 0)

    def test_all_users_and_categories_share_one_inbound_sequence(self):
        db.session.add_all([
            InboundMail(
                ref_no="IN-2026-0001",
                category="GENERAL",
                subject="أول وارد",
                received_date="2026-01-10",
                created_by_id=10,
            ),
            InboundMail(
                ref_no="IN-2026-0001",
                category="FINANCE",
                subject="ثاني وارد",
                received_date="2026-02-10",
                created_by_id=20,
            ),
            CorrCounter(kind="IN", year=2026, category="GENERAL", last_no=1),
            CorrCounter(kind="IN", year=2026, category="FINANCE", last_no=1),
        ])
        db.session.commit()

        first = _corr_next_ref("IN", "2026-03-01", "GENERAL")
        second = _corr_next_ref("IN", "2026-03-02", "FINANCE")

        self.assertEqual(first, "وارد-01032026-000003")
        self.assertEqual(second, "وارد-02032026-000004")
        system_counter = CorrCounter.query.filter_by(
            kind="IN", year=2026, category="SYSTEM"
        ).one()
        self.assertEqual(system_counter.last_no, 4)

    def test_inbound_and_outbound_have_independent_sequences(self):
        inbound_ref = _corr_next_ref("IN", "2027-01-01")
        outbound_ref = _corr_next_ref("OUT", "2027-01-01")

        self.assertEqual(inbound_ref, "وارد-01012027-000001")
        self.assertEqual(outbound_ref, "صادر-01012027-000001")


if __name__ == "__main__":
    unittest.main()
