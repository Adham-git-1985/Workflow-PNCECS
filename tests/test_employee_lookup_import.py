import unittest
from io import BytesIO
from unittest.mock import patch

from flask import Flask

from extensions import db
from models import HRLookupItem, User
from portal.routes import hr_employee_lookups_import
from utils.excel import make_xlsx_bytes


def _unwrapped(function):
    while hasattr(function, "__wrapped__"):
        function = function.__wrapped__
    return function


class EmployeeLookupImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            TESTING=True,
            SECRET_KEY="employee-lookup-import-test",
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(cls.app)
        cls.context = cls.app.app_context()
        cls.context.push()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.context.pop()

    def setUp(self):
        db.drop_all()
        db.create_all()
        self.admin = User(
            email="lookup-admin@example.test",
            name="Lookup Admin",
            password_hash="not-used-in-test",
            role="SUPER_ADMIN",
        )
        db.session.add(self.admin)
        db.session.commit()

    def test_import_accepts_the_downloaded_template_columns(self):
        workbook = make_xlsx_bytes(
            "LOCALITY",
            ["code", "name_ar", "name_en", "sort_order", "is_active"],
            [["AAAA", "شكتمنيتمن", "a;djwlkjwlk", 1, True]],
        )

        import_lookup = _unwrapped(hr_employee_lookups_import)
        with self.app.test_request_context(
            "/portal/hr/masterdata/employee-lookups/LOCALITY/import",
            method="POST",
            content_type="multipart/form-data",
            data={
                "mode": "upsert",
                "file": (BytesIO(workbook), "employee_lookup_LOCALITY_template.xlsx"),
            },
        ), patch("portal.routes.current_user", self.admin), patch(
            "portal.routes._portal_audit"
        ), patch(
            "portal.routes.url_for",
            return_value="/portal/hr/masterdata/employee-lookups/LOCALITY",
        ):
            response = import_lookup("LOCALITY")

        self.assertEqual(response.status_code, 302)
        item = HRLookupItem.query.filter_by(category="LOCALITY", code="AAAA").one()
        self.assertEqual(item.name_ar, "شكتمنيتمن")
        self.assertEqual(item.name_en, "a;djwlkjwlk")
        self.assertEqual(item.sort_order, 1)
        self.assertTrue(item.is_active)


if __name__ == "__main__":
    unittest.main()
