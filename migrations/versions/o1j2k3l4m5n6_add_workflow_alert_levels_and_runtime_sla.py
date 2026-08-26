"""add workflow alert levels and runtime SLA duration

Revision ID: o1j2k3l4m5n6
Revises: n0i1j2k3l4m5
Create Date: 2026-08-26 18:00:00
"""

from alembic import op
import sqlalchemy as sa
from datetime import datetime


revision = "o1j2k3l4m5n6"
down_revision = "n0i1j2k3l4m5"
branch_labels = None
depends_on = None


def _positive_int(value, fallback=None):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _backfill_runtime_sla(bind):
    """Preserve existing SLAs and stop clocks on steps that are still waiting."""
    metadata = sa.MetaData()
    runtime_steps = sa.Table("workflow_instance_steps", metadata, autoload_with=bind)
    instances = sa.Table("workflow_instances", metadata, autoload_with=bind)
    template_steps = sa.Table("workflow_template_steps", metadata, autoload_with=bind)
    templates = sa.Table("workflow_templates", metadata, autoload_with=bind)
    settings = sa.Table("system_setting", metadata, autoload_with=bind)

    system_sla = _positive_int(bind.execute(
        sa.select(settings.c.value).where(settings.c.key == "SLA_DAYS")
    ).scalar(), 3)

    rows = bind.execute(
        sa.select(
            runtime_steps.c.id,
            runtime_steps.c.due_at,
            instances.c.created_at.label("instance_created_at"),
            template_steps.c.sla_days.label("template_step_sla"),
            templates.c.sla_days_default.label("template_default_sla"),
        )
        .select_from(
            runtime_steps
            .join(instances, instances.c.id == runtime_steps.c.instance_id)
            .outerjoin(
                template_steps,
                sa.and_(
                    template_steps.c.template_id == instances.c.template_id,
                    template_steps.c.step_order == runtime_steps.c.step_order,
                ),
            )
            .outerjoin(templates, templates.c.id == instances.c.template_id)
        )
        .where(runtime_steps.c.sla_days.is_(None))
    ).mappings().all()

    for row in rows:
        sla_days = _positive_int(row["template_step_sla"])
        sla_days = sla_days or _positive_int(row["template_default_sla"])
        if not sla_days and isinstance(row["due_at"], datetime) and isinstance(
            row["instance_created_at"], datetime
        ):
            elapsed_days = (
                row["due_at"] - row["instance_created_at"]
            ).total_seconds() / 86400.0
            sla_days = max(1, int(round(elapsed_days))) if elapsed_days > 0 else None
        sla_days = sla_days or system_sla
        bind.execute(
            runtime_steps.update()
            .where(runtime_steps.c.id == row["id"])
            .values(sla_days=sla_days)
        )

    # Old code assigned due_at to every step at request creation. Only the
    # currently active pending step is allowed to keep a running SLA clock.
    active_instance = sa.exists().where(sa.and_(
        instances.c.id == runtime_steps.c.instance_id,
        instances.c.is_completed.is_(False),
        instances.c.current_step_order != runtime_steps.c.step_order,
    ))
    bind.execute(
        runtime_steps.update()
        .where(runtime_steps.c.status == "PENDING", active_instance)
        .values(due_at=None)
    )


def upgrade():
    with op.batch_alter_table("request_escalation") as batch_op:
        batch_op.add_column(
            sa.Column(
                "alert_level",
                sa.Integer(),
                nullable=False,
                server_default="1",
            )
        )

    with op.batch_alter_table("workflow_instance_steps") as batch_op:
        batch_op.add_column(sa.Column("sla_days", sa.Integer(), nullable=True))

    _backfill_runtime_sla(op.get_bind())


def downgrade():
    with op.batch_alter_table("workflow_instance_steps") as batch_op:
        batch_op.drop_column("sla_days")

    with op.batch_alter_table("request_escalation") as batch_op:
        batch_op.drop_column("alert_level")
