"""DeletionPolicy / UpdateReplacePolicy assignments.

The spec defines a fixed table of policies per resource kind. Calling
apply_policy(resource, kind) sets both attributes consistently and fails
loudly on unknown kinds — preventing silent drift.
"""


POLICIES: dict = {
    "rds-instance":                 ("Snapshot", "Snapshot"),
    "s3-ingest-bucket":             ("Retain", "Retain"),
    "db-master-secret":             ("Retain", "Retain"),
    "internal-service-keys-secret": ("Delete", "Delete"),
    "admin-api-key-secret":         ("Retain", "Retain"),
    "sqs-ingest-queue":             ("Delete", "Delete"),
    "alb":                          ("Delete", "Delete"),
    "ecs-cluster":                  ("Delete", "Delete"),
    "log-group":                    ("Delete", "Delete"),
}


def apply_policy(resource, kind: str) -> None:
    """Apply the deletion/replace policies for a given resource kind."""
    if kind not in POLICIES:
        raise ValueError(f"Unknown policy kind: {kind!r}")
    deletion, replace = POLICIES[kind]
    resource.DeletionPolicy = deletion
    resource.UpdateReplacePolicy = replace
