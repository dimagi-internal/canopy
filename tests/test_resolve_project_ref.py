"""`--project` resolves to a reference that names exactly ONE project.

A `P<N>` is unique per creating agent, not across an agent's board: its own P5 and a
P5 another agent made that it works on are listed side by side, and canopy-web reads a
bare "P5" as the agent's own. Turning the name "Canopy support" into "P5" would file
hal's task into its own ARCHIVED P5, Supply, with no error.
"""
import click
import pytest

from orchestrator.agent_cli import resolve_project_ref


class FakeClient:
    def __init__(self, projects):
        self.projects = projects

    def list_projects(self):
        return self.projects


OWN_ARCHIVED = {"ext_id": "P5", "name": "Supply", "address": "connect/supply", "handle": "supply"}
SHARED_LIVE = {"ext_id": "P5", "name": "Canopy support", "address": "dimagi/canopy-support",
               "handle": "canopy-support"}
OTHER = {"ext_id": "P3", "name": "connect-labs reliability", "address": "connect/connect-labs-reliability",
         "handle": "connect-labs-reliability"}
BOARD = FakeClient([OWN_ARCHIVED, SHARED_LIVE, OTHER])


def test_a_name_whose_alias_is_shared_resolves_to_its_address():
    assert resolve_project_ref(BOARD, "Canopy support") == "dimagi/canopy-support"
    assert resolve_project_ref(BOARD, "supply") == "connect/supply"


def test_a_bare_shared_alias_is_refused_with_the_addresses():
    with pytest.raises(click.ClickException) as exc:
        resolve_project_ref(BOARD, "p5")
    msg = exc.value.message
    assert "dimagi/canopy-support" in msg and "connect/supply" in msg and "Nothing was written" in msg


def test_an_unshared_alias_still_resolves_to_itself():
    assert resolve_project_ref(BOARD, "P3") == "P3"
    assert resolve_project_ref(BOARD, "connect-labs reliability") == "P3"
    assert resolve_project_ref(FakeClient([SHARED_LIVE]), "Canopy support") == "P5"


def test_an_address_names_its_project_even_when_the_alias_is_shared():
    assert resolve_project_ref(BOARD, "dimagi/canopy-support") == "dimagi/canopy-support"
