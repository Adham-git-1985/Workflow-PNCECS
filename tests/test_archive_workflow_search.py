import tempfile
import unittest

from flask import Flask
from flask_login import LoginManager, login_user, logout_user

from archive import archive_bp
from archive import routes as archive_routes
from extensions import db
from models import ArchivedFile, RequestAttachment, User, WorkflowRequest


class ArchiveWorkflowSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.app = Flask(__name__, instance_path=cls.temp_dir.name)
        cls.app.config.update(
            TESTING=True,
            SECRET_KEY="archive-workflow-search-test",
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(cls.app)

        login_manager = LoginManager()
        login_manager.init_app(cls.app)

        @login_manager.user_loader
        def load_user(user_id):
            try:
                return db.session.get(User, int(user_id))
            except (TypeError, ValueError):
                return None

        cls.app.register_blueprint(archive_bp)
        cls.context = cls.app.app_context()
        cls.context.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        cls.context.pop()
        cls.temp_dir.cleanup()

    def setUp(self):
        db.session.remove()
        db.drop_all()
        db.create_all()

        self.admin = User(
            email="archive-admin@example.test",
            name="Archive Admin",
            password_hash="not-used-in-test",
            role="ADMIN",
        )
        db.session.add(self.admin)
        db.session.flush()

        self.first_request = WorkflowRequest(
            title="مراسلة أولى",
            description="محتوى المسار الأول",
            status="DRAFT",
            requester_id=self.admin.id,
        )
        self.second_request = WorkflowRequest(
            title="مراسلة ثانية",
            description="محتوى المسار الثاني",
            status="IN_PROGRESS",
            requester_id=self.admin.id,
        )
        db.session.add_all((self.first_request, self.second_request))
        db.session.flush()

        self.first_file = self._attachment(
            self.first_request,
            "report-first.pdf",
        )
        self.second_file = self._attachment(
            self.second_request,
            "report-second.pdf",
        )
        self.attachment_match_file = self._attachment(
            self.second_request,
            "invoice-archive.pdf",
        )
        db.session.commit()

    def _attachment(self, workflow_request, filename):
        archived_file = ArchivedFile(
            original_name=filename,
            stored_name=filename,
            file_path=f"storage/archive/{filename}",
            owner_id=self.admin.id,
        )
        db.session.add(archived_file)
        db.session.flush()
        db.session.add(RequestAttachment(
            request_id=workflow_request.id,
            archived_file_id=archived_file.id,
        ))
        return archived_file

    def _search(self, filters):
        with self.app.test_request_context("/archive/my-files"):
            login_user(self.admin)
            try:
                return archive_routes._search_workflows_from_archive(filters)
            finally:
                logout_user()

    def test_text_search_includes_attachment_names_but_not_status_or_request_number(self):
        by_title = self._search({"workflow_q": "أولى", "workflow_status": ""})
        self.assertEqual([row.id for row in by_title], [self.first_request.id])

        by_content = self._search({"workflow_q": "الثاني", "workflow_status": ""})
        self.assertEqual([row.id for row in by_content], [self.second_request.id])

        matching = self._search({"workflow_q": "invoice", "workflow_status": ""})
        self.assertEqual([row.id for row in matching], [self.second_request.id])

        by_status = self._search({"workflow_q": "DRAFT", "workflow_status": ""})
        self.assertEqual(by_status, [])

        by_number = self._search({
            "workflow_q": str(self.second_request.id),
            "workflow_status": "",
        })
        self.assertEqual(by_number, [])

    def test_multiple_workflow_results_require_a_selection_and_filter_to_it(self):
        filters = {"workflow_q": "report", "workflow_status": ""}
        workflows = self._search(filters)
        self.assertEqual({row.id for row in workflows}, {
            self.first_request.id,
            self.second_request.id,
        })

        without_selection = archive_routes._filter_files_to_workflow_results(
            ArchivedFile.query,
            filters,
            workflows,
        ).all()
        self.assertEqual(without_selection, [])

        selected_filters = {
            **filters,
            "workflow_request_id": self.second_request.id,
        }
        selected_files = archive_routes._filter_files_to_workflow_results(
            ArchivedFile.query,
            selected_filters,
            workflows,
        ).all()
        self.assertEqual(
            {row.id for row in selected_files},
            {self.second_file.id, self.attachment_match_file.id},
        )


if __name__ == "__main__":
    unittest.main()
