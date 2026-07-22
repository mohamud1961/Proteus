from aether_next.kernel_turns import _update_world_from_receipt
from aether_next.ledger import ExecutionLedger, Receipt
from aether_next.task_contract import TaskClause, TaskContract
from aether_next.world import WorldState


def _world():
    contract = TaskContract.create("Run a service.", (TaskClause("service", "service is healthy"),))
    return WorldState(contract), ExecutionLedger()


def test_production_process_receipt_fields_project_to_worldstate_lifecycle():
    world, ledger = _world()
    launch = Receipt(
        "launch", 1, "process_launch", True, "launched web",
        payload={"process_id": "proc-1", "service_name": "web", "live": True},
    )
    _update_world_from_receipt(world, launch, step=1, ledger=ledger)
    assert world.services["web"]["state"] == "running"

    probe = Receipt(
        "probe", 2, "service_probe", True, "web live",
        payload={"target": "web", "service_name": "web", "live": True},
    )
    _update_world_from_receipt(world, probe, step=2, ledger=ledger)
    assert world.services["web"]["state"] == "ready"
    assert world.services["web"]["readiness"] is True

    stop = Receipt(
        "stop", 3, "process_stop", True, "stopped web",
        payload={"process_id": "proc-1", "service_name": "web", "live": False},
    )
    _update_world_from_receipt(world, stop, step=3, ledger=ledger)
    assert world.services["web"]["state"] == "stopped"
