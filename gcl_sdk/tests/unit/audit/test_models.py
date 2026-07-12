import datetime
import uuid

from gcl_sdk.audit import constants
from gcl_sdk.audit.dm import models


def _event():
    timestamp = datetime.datetime(2026, 7, 12, 12, 0, tzinfo=datetime.timezone.utc)
    return models.AuditDeliveryEvent(
        uuid=uuid.uuid4(),
        service_name="compute",
        resource_type="node",
        resource_uuid=uuid.uuid4(),
        project_id=uuid.uuid4(),
        actor_user_uuid=None,
        action="create",
        snapshot={"status": "NEW"},
        created_at=timestamp,
        updated_at=timestamp,
    )


def test_dump_to_delivery_snapshot_excludes_local_delivery_state():
    event = _event()

    snapshot = event.dump_to_delivery_snapshot()

    assert set(snapshot) == set(constants.INGEST_FIELDS)
    assert "status" not in snapshot
    assert snapshot["uuid"] == str(event.uuid)
    assert snapshot["created_at"] == "2026-07-12 12:00:00.000000"


def test_base_model_matches_ingest_contract():
    assert set(models.AuditEventBase.properties) == set(constants.INGEST_FIELDS)
