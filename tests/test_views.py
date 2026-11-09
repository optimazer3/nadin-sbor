import re
from datetime import date

from bot.models import Action, ActionType, Status, Task
from bot.views import order_tasks, plan_text, split_message, task_card, task_list_messages

TODAY = date(2026, 11, 9)  # понедельник
EMOJI = re.compile("[\U0001f300-\U0001faff☀-➿⬀-⯿⏩-⏺]")


def make(id, title, assignee=None, deadline=None, status=Status.NEW):
    return Task(id=id, title=title, assignee=assignee, deadline=deadline, status=status)


def test_list_is_plain_numbered_paragraphs():
    tasks = [
        make(30, "Отчёт", "Иван Петров", date(2026, 11, 5), Status.IN_PROGRESS),
        make(7, "Звонок", None, None, Status.ON_HOLD),
        make(12, "Прайс", "Мария", None),
        make(5, "Счета", None, date(2027, 1, 15)),
        make(40, "Закрыто", "Иван", None, Status.DONE),  # выполненная — не на контроле
    ]
    active = [t for t in tasks if t.is_active]
    order = [t.id for t in order_tasks(active)]
    assert order == [5, 7, 12, 30]  # в порядке добавления, без группировки
    number = {task_id: n for n, task_id in enumerate(order, 1)}.__getitem__

    [msg] = task_list_messages(active, TODAY, number=number)
    assert msg == (
        "<b>Задачи на контроле</b>\n\n"
        "1. Счета\nдо 15 января 2027\n\n"
        "2. Звонок\n\n"
        "3. Прайс\nМария\n\n"
        "4. Отчёт\nИван Петров, до 5 ноября"
    )
    assert not EMOJI.search(msg)
    # Не пишем того, чего нет, и никаких подсказок/статистики.
    for noise in ("не указан", "без срока", "Всего", "просроч", "задача 1 выполнена", "Без исполнителя"):
        assert noise not in msg


def test_empty_list_and_escaping():
    assert task_list_messages([], TODAY) == ["<b>Задачи на контроле</b>\n\nЗадач нет."]
    [msg] = task_list_messages([make(1, "<b>x</b>", "A&B")], TODAY)
    assert "1. &lt;b&gt;x&lt;/b&gt;\nA&amp;B" in msg


def test_long_list_is_split():
    tasks = [make(i, "Длинная задача " * 15, "Иван") for i in range(1, 80)]
    messages = task_list_messages(tasks, TODAY)
    assert len(messages) > 1 and all(len(m) <= 4000 for m in messages)
    joined = "\n\n".join(messages)
    assert all(f"\n{i}. Длинная" in joined for i in range(1, 80))


def test_task_card():
    assert task_card(make(5, "Отчёт", "Иван", date(2026, 11, 13)), TODAY, lambda _: 2) == (
        "<b>Задача 2</b>\n\nОтчёт\nИван, до 13 ноября"
    )
    assert task_card(make(5, "Отчёт", status=Status.DONE), TODAY).endswith("Отчёт\nвыполнена")


def test_plan_texts():
    tasks = {
        7: make(7, "Прайс", "Мария", date(2026, 11, 5)),
        5: make(5, "Отчёт", "Иван", None, Status.IN_PROGRESS),
    }
    number = {7: 1, 5: 2}.__getitem__
    create = Action(ActionType.CREATE, title="Звонок", assignee="Мария", deadline=date(2026, 11, 10))
    assert plan_text([create], tasks, TODAY, number) == "Добавить задачу?\n\nЗвонок\nМария, до 10 ноября"
    assert plan_text([Action(ActionType.CREATE, title="Вода")], tasks, TODAY) == "Добавить задачу?\n\nВода"

    move = Action(ActionType.UPDATE, task_ids=[7], deadline=date(2026, 11, 20), assignee="Иван")
    assert plan_text([move], tasks, TODAY, number) == (
        "Изменить задачу 1?\n\nПрайс\nИсполнитель: Мария → Иван\nСрок: до 5 ноября → до 20 ноября"
    )
    done = Action(ActionType.UPDATE, task_ids=[5], status=Status.DONE)
    assert plan_text([done], tasks, TODAY, number) == "Отметить задачу 2 выполненной?\n\nОтчёт"

    mixed = [
        Action(ActionType.DELETE, task_ids=[7]),
        Action(ActionType.SET_SCHEDULE, weekday="fri", time="09:00"),
    ]
    assert plan_text(mixed, tasks, TODAY, number) == (
        "Я правильно понял?\n\n1. Удалить задачу 1\nПрайс\n\n2. Присылать список каждую пятницу в 09:00"
    )


def test_split_message_respects_limit():
    blocks = ["a" * 30, "b" * 30, "c\n" * 40]
    out = split_message(blocks, limit=50)
    assert all(len(m) <= 50 for m in out)
    assert "".join(out).count("c") == 40
