# workflow/paths_models.py
# Shim file to avoid duplicate models:
# Keep your real models only in models.py

from models import (
    WorkflowTemplate,
    WorkflowTemplateStep,
    WorkflowInstance,
    WorkflowInstanceStep,
    RequestAttachment,
)

__all__ = [
    "WorkflowTemplate",
    "WorkflowTemplateStep",
    "WorkflowInstance",
    "WorkflowInstanceStep",
    "RequestAttachment",
]

