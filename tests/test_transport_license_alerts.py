import tempfile
import unittest
from datetime import date, timedelta

from flask import Flask

from extensions import db
from models import (
    Notification,
    NotificationEmailDelivery,
    SystemSetting,
    TransportDriver,
    TransportVehicle,
    User,
)
from portal.transport_license_alerts_job import check_transport_license_expirations


class TransportLicenseAlertsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.app = Flask(__name__, instance_path=cls.temp_dir.name)
        cls.app.config.update(
            TESTING=True,
            SECRET_KEY="transport-licence-alerts-test",
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
        cls.temp_dir.cleanup()

    def setUp(self):
        db.session.remove()
        db.drop_all()
        db.create_all()

        self.manager = User(
            email="fleet-manager@example.test",
            name="Fleet manager",
            password_hash="unused",
            role="SUPER_ADMIN",
        )
        self.driver_user = User(
            email="driver@example.test",
            name="Driver user",
            password_hash="unused",
            role="EMPLOYEE",
        )
        db.session.add_all((self.manager, self.driver_user))
        db.session.flush()
        db.session.add(SystemSetting(
            key="TRANSPORT_MANAGER_USER_ID",
            value=str(self.manager.id),
        ))

        self.today = date(2026, 8, 30)
        expiry_day = (self.today + timedelta(days=14)).isoformat()
        self.driver = TransportDriver(
            user_id=self.driver_user.id,
            name="Test driver",
            license_no="D-100",
            license_end_day=expiry_day,
            status="ACTIVE",
        )
        self.vehicle = TransportVehicle(
            plate_no="31-456-78",
            label="Test vehicle",
            license_end_day=expiry_day,
            status="ACTIVE",
        )
        db.session.add_all((self.driver, self.vehicle))
        db.session.commit()

    def test_alerts_are_queued_once_for_each_expiring_licence(self):
        self.assertEqual(check_transport_license_expirations(self.today), 2)

        notifications = Notification.query.order_by(Notification.id).all()
        self.assertEqual(len(notifications), 3)
        self.assertEqual(NotificationEmailDelivery.query.count(), 3)
        self.assertTrue(any("رخصة سائق" in row.message for row in notifications))
        self.assertTrue(any("رخصة مركبة" in row.message for row in notifications))
        self.assertEqual(self.driver.license_alert_sent_for, self.driver.license_end_day)
        self.assertEqual(self.vehicle.license_alert_sent_for, self.vehicle.license_end_day)

        self.assertEqual(check_transport_license_expirations(self.today), 0)
        self.assertEqual(Notification.query.count(), 3)
        self.assertEqual(NotificationEmailDelivery.query.count(), 3)


if __name__ == "__main__":
    unittest.main()
