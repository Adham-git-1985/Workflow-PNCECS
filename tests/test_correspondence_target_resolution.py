import unittest

from flask import Flask
from sqlalchemy import event

from extensions import db
from models import (
    Department,
    Directorate,
    Division,
    Organization,
    OrgNode,
    OrgNodeAssignment,
    OrgNodeManager,
    OrgNodeType,
    OrgUnitAssignment,
    OrgUnitManager,
    Section,
    Unit,
    User,
)
from services.correspondence_workflow import (
    _correspondence_target_key,
    bulk_correspondence_target_user_ids,
    correspondence_actor_target_keys,
    correspondence_target_user_ids,
)


class CorrespondenceTargetResolutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY="correspondence-target-test",
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
        db.session.remove()
        db.drop_all()
        db.create_all()

    @staticmethod
    def _new_user(name, **fields):
        return User(
            email=f"{name}@example.test",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
            **fields,
        )

    def _seed_targets(self):
        organization = Organization(name_ar="Organization")
        directorate = Directorate(name_ar="Directorate", organization=organization)
        unit = Unit(name_ar="Unit", organization=organization)
        directorate_department = Department(
            name_ar="Directorate department",
            directorate=directorate,
        )
        unit_department = Department(name_ar="Unit department", unit=unit)
        section = Section(name_ar="Section", department=directorate_department)
        division = Division(name_ar="Division", section=section)
        node_type = OrgNodeType(code="TEST_NODE", name_ar="Node type")
        node = OrgNode(name_ar="Node", type=node_type)
        db.session.add_all((
            organization,
            directorate,
            unit,
            directorate_department,
            unit_department,
            section,
            division,
            node_type,
            node,
        ))
        db.session.flush()

        direct_user = self._new_user("direct-user")
        assigned_user = self._new_user("assigned-user")
        manager_user = self._new_user("manager-user")
        multi_direct_user = self._new_user(
            "multi-direct-user",
            directorate_id=directorate.id,
            unit_id=unit.id,
            section_id=section.id,
            division_id=division.id,
            org_node_id=node.id,
        )
        directorate_descendant = self._new_user(
            "directorate-descendant",
            department_id=directorate_department.id,
        )
        unit_descendant = self._new_user(
            "unit-descendant",
            department_id=unit_department.id,
        )
        db.session.add_all((
            direct_user,
            assigned_user,
            manager_user,
            multi_direct_user,
            directorate_descendant,
            unit_descendant,
        ))
        db.session.flush()

        db.session.add(OrgNodeAssignment(
            node_id=node.id,
            user_id=assigned_user.id,
        ))
        db.session.add(OrgNodeManager(
            node_id=node.id,
            manager_user_id=manager_user.id,
            deputy_user_id=multi_direct_user.id,
        ))

        unit_targets = (
            ("ORGANIZATION", organization.id),
            ("DIRECTORATE", directorate.id),
            ("UNIT", unit.id),
            ("DEPARTMENT", directorate_department.id),
            ("SECTION", section.id),
            ("DIVISION", division.id),
            ("TEAM", 777),
        )
        for unit_type, unit_id in unit_targets:
            if unit_type != "UNIT":
                db.session.add(OrgUnitAssignment(
                    unit_type=unit_type,
                    unit_id=unit_id,
                    user_id=assigned_user.id,
                ))
            db.session.add(OrgUnitManager(
                unit_type=unit_type.lower(),
                unit_id=unit_id,
                manager_user_id=manager_user.id,
                deputy_user_id=multi_direct_user.id,
            ))
        db.session.add(OrgUnitManager(
            unit_type="custom",
            unit_id=888,
            manager_user_id=manager_user.id,
            deputy_user_id=multi_direct_user.id,
        ))
        db.session.commit()

        return {
            "direct_user_id": direct_user.id,
            "organization_id": organization.id,
            "directorate_id": directorate.id,
            "unit_id": unit.id,
            "department_id": directorate_department.id,
            "section_id": section.id,
            "division_id": division.id,
            "node_id": node.id,
        }

    @staticmethod
    def _query_stats(callable_):
        query_count = 0
        max_parameter_count = 0

        def count_queries(conn, cursor, statement, parameters, context, executemany):
            nonlocal query_count, max_parameter_count
            query_count += 1
            try:
                max_parameter_count = max(max_parameter_count, len(parameters))
            except TypeError:
                pass

        event.listen(db.engine, "before_cursor_execute", count_queries)
        try:
            result = callable_()
        finally:
            event.remove(db.engine, "before_cursor_execute", count_queries)
        return query_count, max_parameter_count, result

    def test_bulk_results_match_scalar_and_queries_are_bounded(self):
        target_ids = self._seed_targets()
        targets = [
            {"kind": " user ", "id": str(target_ids["direct_user_id"])},
            {"kind": "org_node", "id": target_ids["node_id"]},
            {"kind": "ORG", "id": target_ids["organization_id"]},
            {"kind": "organization", "id": target_ids["organization_id"]},
            {"kind": "directorate", "id": target_ids["directorate_id"]},
            {"kind": "unit", "id": target_ids["unit_id"]},
            {"kind": "department", "id": target_ids["department_id"]},
            {"kind": "section", "id": target_ids["section_id"]},
            {"kind": "division", "id": target_ids["division_id"]},
            {"kind": "team", "id": 777},
            {"kind": "custom", "id": 888},
            {"kind": "UNIT", "id": str(target_ids["unit_id"])},
            {"kind": "UNIT", "id": "not-an-id"},
            None,
        ]
        expected = {
            target_key: correspondence_target_user_ids(target)
            for target in targets
            if (target_key := _correspondence_target_key(target)) is not None
        }

        query_count, _max_parameter_count, actual = self._query_stats(
            lambda: bulk_correspondence_target_user_ids(targets)
        )

        self.assertEqual(actual, expected)
        self.assertLessEqual(query_count, 7)
        self.assertEqual(
            actual[("ORG", target_ids["organization_id"])],
            actual[("ORGANIZATION", target_ids["organization_id"])],
        )
        self.assertEqual(
            actual[("CUSTOM", 888)],
            correspondence_target_user_ids({"kind": "CUSTOM", "id": 888}),
        )

        many_targets = list(targets)
        for target_id in range(1000, 1600):
            many_targets.extend((
                {"kind": "ORG_NODE", "id": target_id},
                {"kind": "DIRECTORATE", "id": target_id},
                {"kind": "UNIT", "id": target_id},
                {"kind": "DEPARTMENT", "id": target_id},
                {"kind": "TEAM", "id": target_id},
            ))
        self.assertGreater(len(many_targets), 3000)

        many_query_count, max_parameter_count, _result = self._query_stats(
            lambda: bulk_correspondence_target_user_ids(many_targets)
        )
        self.assertLessEqual(many_query_count, 30)
        self.assertLess(many_query_count, len(many_targets) // 50)
        self.assertLessEqual(max_parameter_count, 800)

    def test_actor_target_keys_are_the_inverse_of_scalar_resolution(self):
        target_ids = self._seed_targets()
        users = User.query.order_by(User.id.asc()).all()
        targets = [
            *({"kind": "USER", "id": user.id} for user in users),
            {"kind": "ORG_NODE", "id": target_ids["node_id"]},
            {"kind": "ORG", "id": target_ids["organization_id"]},
            {"kind": "ORGANIZATION", "id": target_ids["organization_id"]},
            {"kind": "DIRECTORATE", "id": target_ids["directorate_id"]},
            {"kind": "UNIT", "id": target_ids["unit_id"]},
            {"kind": "DEPARTMENT", "id": target_ids["department_id"]},
            {"kind": "SECTION", "id": target_ids["section_id"]},
            {"kind": "DIVISION", "id": target_ids["division_id"]},
            {"kind": "TEAM", "id": 777},
            {"kind": "CUSTOM", "id": 888},
            {"kind": "DEPARTMENT", "id": 999999},
        ]

        for user in users:
            target_keys = correspondence_actor_target_keys([user.id])
            for target in targets:
                target_key = _correspondence_target_key(target)
                self.assertEqual(
                    target_key in target_keys,
                    user.id in correspondence_target_user_ids(target),
                    (user.email, target_key),
                )
            for target_key in target_keys:
                self.assertIn(
                    user.id,
                    correspondence_target_user_ids({
                        "kind": target_key[0],
                        "id": target_key[1],
                    }),
                    (user.email, target_key),
                )

        actor_ids = [user.id for user in users]
        query_count, max_parameter_count, target_keys = self._query_stats(
            lambda: correspondence_actor_target_keys(actor_ids)
        )
        self.assertLessEqual(query_count, 6)
        # The two manager lookups match both manager and deputy columns, so
        # one actor-id chunk legitimately appears twice in those predicates.
        # Keep the bound explicit: chunks are 400 IDs, therefore at most 800
        # SQLite bind parameters rather than one query per correspondence row.
        self.assertLessEqual(max_parameter_count, len(actor_ids) * 2)
        for target in targets:
            target_key = _correspondence_target_key(target)
            self.assertEqual(
                target_key in target_keys,
                bool(set(actor_ids).intersection(correspondence_target_user_ids(target))),
                target_key,
            )


if __name__ == "__main__":
    unittest.main()
