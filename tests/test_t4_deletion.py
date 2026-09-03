from __future__ import annotations

from pathlib import Path

import pytest

from readpaper.deletion import DeletionService
from readpaper.errors import ErrorCode, ReadPaperError
from readpaper.ids import bundle_id, paper_id
from readpaper.models import Actor, RunState
from readpaper.state import StateService


PAPER = paper_id(b"delete")
BUNDLE = bundle_id(schema_version=1, paper_id=PAPER, landing_url=None, artifacts=[])


def prepared(tmp_path: Path) -> tuple[StateService, DeletionService, str]:
    state = StateService(tmp_path)
    run = state.create_run(task_id="task", paper_id=PAPER, bundle_id=BUNDLE)
    return state, DeletionService(tmp_path), run.run_id


def test_preview_is_not_approval_and_active_run_blocks_execute(tmp_path: Path) -> None:
    _, deletion, _ = prepared(tmp_path)
    request = deletion.create_preview(task_id="task", paper_id=PAPER, client_request_id="cr_" + "1" * 32)
    assert request["state"] == "created"
    with pytest.raises(ReadPaperError) as not_presented:
        deletion.execute(
            request_id=request["deletion_request_id"],
            task_id="task",
            approval_text=f"DELETE {PAPER} {request['deletion_request_id']}",
            approval_turn_event_id="hev_" + "1" * 64,
        )
    assert not_presented.value.code is ErrorCode.DELETE_CONFIRMATION_REQUIRED
    with pytest.raises(ReadPaperError):
        deletion.mark_presented(
            request_id=request["deletion_request_id"],
            actual_message=request["preview_text"] + "changed",
            host_event_id="hev_" + "2" * 64,
        )
    deletion.mark_presented(
        request_id=request["deletion_request_id"],
        actual_message=request["preview_text"],
        host_event_id="hev_" + "2" * 64,
    )
    with pytest.raises(ReadPaperError) as blocked:
        deletion.execute(
            request_id=request["deletion_request_id"],
            task_id="task",
            approval_text=f"DELETE {PAPER} {request['deletion_request_id']}",
            approval_turn_event_id="hev_" + "3" * 64,
        )
    assert blocked.value.code is ErrorCode.DELETE_CONFIRMATION_REQUIRED


def test_paused_run_exact_preview_and_confirmation_delete_and_clear_binding(tmp_path: Path) -> None:
    state, deletion, run_id = prepared(tmp_path)
    state.transition(
        task_id="task", paper_id=PAPER, run_id=run_id, to_state=RunState.PAUSED,
        actor=Actor.USER, reason_code="pause",
    )
    request = deletion.create_preview(task_id="task", paper_id=PAPER, client_request_id="cr_" + "2" * 32)
    request_id = request["deletion_request_id"]
    deletion.mark_presented(
        request_id=request_id,
        actual_message=request["preview_text"],
        host_event_id="hev_" + "4" * 64,
    )
    with pytest.raises(ReadPaperError):
        deletion.execute(
            request_id=request_id,
            task_id="task",
            approval_text=f"DELETE {PAPER} wrong",
            approval_turn_event_id="hev_" + "5" * 64,
        )
    response = deletion.execute(
        request_id=request_id,
        task_id="task",
        approval_text=f"DELETE {PAPER} {request_id}",
        approval_turn_event_id="hev_" + "5" * 64,
    )
    assert response["status"] == "deleted"
    assert not (tmp_path / "papers" / PAPER).exists()
    binding = state.get_binding("task")
    assert binding.current_paper_id is None
    assert deletion.execute(
        request_id=request_id,
        task_id="task",
        approval_text="ignored on completed replay",
        approval_turn_event_id="ignored",
    ) == response


def test_scope_change_invalidates_presented_request(tmp_path: Path) -> None:
    state, deletion, run_id = prepared(tmp_path)
    state.transition(
        task_id="task", paper_id=PAPER, run_id=run_id, to_state=RunState.PAUSED,
        actor=Actor.USER, reason_code="pause",
    )
    request = deletion.create_preview(task_id="task", paper_id=PAPER, client_request_id="cr_" + "3" * 32)
    deletion.mark_presented(
        request_id=request["deletion_request_id"], actual_message=request["preview_text"],
        host_event_id="hev_" + "6" * 64,
    )
    # A new run changes the project-wide deletion scope.
    state.create_run(task_id="other-task", paper_id=PAPER, bundle_id=BUNDLE)
    with pytest.raises(ReadPaperError) as changed:
        deletion.execute(
            request_id=request["deletion_request_id"], task_id="task",
            approval_text=f"DELETE {PAPER} {request['deletion_request_id']}",
            approval_turn_event_id="hev_" + "7" * 64,
        )
    assert changed.value.code is ErrorCode.DELETE_SCOPE_CHANGED


def test_symlink_paper_boundary_is_rejected(tmp_path: Path) -> None:
    StateService(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    paper_path = tmp_path / "papers" / PAPER
    paper_path.symlink_to(outside, target_is_directory=True)
    deletion = DeletionService(tmp_path)
    with pytest.raises(ReadPaperError) as error:
        deletion.create_preview(task_id="task", paper_id=PAPER, client_request_id="cr_" + "4" * 32)
    assert error.value.code is ErrorCode.ID_MISMATCH
