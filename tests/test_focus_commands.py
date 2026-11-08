from datetime import date

from bot.commands import apply_plan, match_team
from bot.focus import FocusStore
from bot.models import Action, ActionType, Status, TaskDraft
from bot.repository import InMemoryTaskRepository


def test_focus_expires_and_drops():
    now = [0.0]
    focus = FocusStore(ttl=60, clock=lambda: now[0])
    focus.set(1, 5)
    assert focus.get(1) == 5
    now[0] = 61
    assert focus.get(1) is None  # давно открыта — «эта задача» уже не про неё
    focus.set(1, 7)
    focus.drop(1, task_id=5)  # удалили другую задачу — фокус остаётся
    assert focus.get(1) == 7
    focus.drop(1, task_id=7)
    assert focus.get(1) is None


def test_match_team():
    team = ("Иван", "Мария")
    assert match_team(" иван ", team) == "Иван"
    assert match_team("Пётр", team) == "Пётр"
    assert match_team(None, team) is None


class FlakyRepo(InMemoryTaskRepository):
    def __init__(self):
        super().__init__()
        self.fail_create = False
        self.fail_update_ids: set[int] = set()

    async def create_many(self, drafts, **kwargs):
        if self.fail_create:
            raise RuntimeError("db down")
        return await super().create_many(drafts, **kwargs)

    async def update(self, task_id, **kwargs):
        if task_id in self.fail_update_ids:
            raise RuntimeError("db down")
        return await super().update(task_id, **kwargs)


async def test_apply_plan_partial_failure_reports_each_action():
    repo = FlakyRepo()
    a, b = await repo.create_many(
        [TaskDraft("A"), TaskDraft("B")], source="text", raw_text=None, created_by=1
    )
    repo.fail_update_ids = {a.id}
    plan = [
        Action(ActionType.UPDATE, task_ids=[a.id, b.id], status=Status.DONE),
        Action(ActionType.DELETE, task_ids=[99]),
    ]
    result = await apply_plan(repo, plan, source="voice", raw_text="x", created_by=1)
    assert result.errors == 1
    assert [t.id for t in result.updated] == [b.id]
    assert repo.tasks[b.id].status == Status.DONE and repo.tasks[a.id].status == Status.NEW
    assert any("ошибка базы данных" in line for line in result.lines)
    assert any("#99 не найдена" in line for line in result.lines)


async def test_apply_plan_all_failed():
    repo = FlakyRepo()
    repo.fail_create = True
    plan = [Action.create(TaskDraft("A", deadline=date(2026, 11, 13)))]
    result = await apply_plan(repo, plan, source="text", raw_text="A", created_by=1)
    assert result.nothing_done and result.errors == 1
