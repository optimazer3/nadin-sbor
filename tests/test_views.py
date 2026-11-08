from datetime import date

from bot.models import Status, Task, TaskDraft
from bot.views import dashboard, draft_text, order_tasks, split_message, task_list_messages

TODAY = date(2026, 11, 9)  # понедельник


def make(id, title, assignee=None, deadline=None, status=Status.NEW):
    return Task(id=id, title=title, assignee=assignee, deadline=deadline, status=status)


def test_dashboard_empty():
    [msg] = dashboard([make(1, "Готово", status=Status.DONE)], TODAY)
    assert "Активных задач нет" in msg


def test_dashboard_groups_and_flags():
    tasks = [
        make(1, "Отчёт", "Иван", date(2026, 11, 5), Status.IN_PROGRESS),  # просрочено
        make(2, "Прайс", "Мария", date(2026, 11, 9)),  # сегодня
        make(3, "Звонок", "Иван", date(2026, 11, 12)),
        make(4, "Без срока", None, None, Status.ON_HOLD),
        make(5, "Закрыто", "Иван", None, Status.DONE),  # не активная
        make(6, "<script>", "Мария"),
    ]
    [msg] = dashboard(tasks, TODAY)
    assert "Активных задач: <b>5</b>" in msg
    assert "❗ Просрочено: <b>1</b>" in msg
    assert "⏰ Срок на этой неделе: 2" in msg
    assert "Без срока: 2" in msg
    assert "Закрыто" not in msg
    assert "&lt;script&gt;" in msg and "<script>" not in msg
    # Иван раньше Марии, «Без исполнителя» в конце; внутри группы — по сроку.
    ivan, maria = "👤 <b>Иван</b> — 2 задачи", "👤 <b>Мария</b> — 2 задачи"
    assert msg.index(ivan) < msg.index(maria) < msg.index("Без исполнителя")
    assert msg.index("<b>1.</b> Отчёт") < msg.index("<b>3.</b> Звонок")
    assert "просрочено на 4 дня" in msg
    assert "<b>2.</b> Прайс  ·  до пн, 09.11  ⏰ сегодня" in msg


def test_dashboard_splits_long_output():
    tasks = [make(i, "Очень длинная задача " * 10, f"Сотрудник {i % 7}") for i in range(1, 120)]
    messages = dashboard(tasks, TODAY)
    assert len(messages) > 1
    assert all(len(m) <= 4000 for m in messages)
    joined = "\n".join(messages)
    assert all(f"<b>{i}.</b> " in joined for i in range(1, 120))


def test_split_message_respects_limit():
    blocks = ["a" * 30, "b" * 30, "c\n" * 40]
    out = split_message(blocks, limit=50)
    assert all(len(m) <= 50 for m in out)
    assert "".join(out).count("c") == 40


def test_draft_text_single_and_multi():
    one = draft_text([TaskDraft("Отчёт", "Иван", date(2026, 11, 13))], TODAY, recognized="Ивану отчёт")
    assert "Вы сказали" in one and "Я правильно понял задачу?" in one
    assert "13.11.2026 (пт) — через 4 дня" in one
    many = draft_text([TaskDraft("A"), TaskDraft("B")], TODAY, recognized=None)
    assert "Задач: 2" in many and "<b>2.</b>" in many and "Вы сказали" not in many


def test_task_list_groups_by_person_with_sequential_numbers():
    team = ("Иван Петров", "Мария Сидорова")
    tasks = [
        make(7, "Звонок", None, None, Status.ON_HOLD),
        make(30, "Отчёт", "Иван Петров", date(2026, 11, 5), Status.IN_PROGRESS),
        make(12, "Прайс", "мария", date(2026, 11, 20)),
        make(5, "Счета", "иван", date(2026, 11, 10)),  # тот же Иван Петров
    ]
    order = [t.id for t in order_tasks(tasks, team)]
    assert order == [30, 5, 12, 7]  # по людям, внутри по сроку, «без исполнителя» в конце
    number = {task_id: n for n, task_id in enumerate(order, 1)}.__getitem__

    [msg] = task_list_messages(tasks, TODAY, "📋 <b>Задачи</b>", number, team)
    assert msg.startswith("📋 <b>Задачи</b>\nВсего: 4 задачи")
    # Один человек — одна группа, даже если имя записано по-разному.
    assert msg.count("Иван Петров") == 1 and "👤 <b>Иван Петров</b> — 2 задачи" in msg
    assert "👤 <b>Мария Сидорова</b> — 1 задача" in msg
    assert "🙋 <b>Без исполнителя</b> — 1 задача" in msg
    assert msg.index("<b>1.</b> Отчёт") < msg.index("<b>2.</b> Счета") < msg.index("<b>3.</b> Прайс")
    assert "<b>4.</b> Звонок\n      📅 без срока  ·  ⏸ На паузе" in msg
    assert "📅 до чт, 05.11  ❗ просрочено на 4 дня  ·  🔄 В работе" in msg
    assert "«задача 1 выполнена»" in msg
    assert task_list_messages([], TODAY, "T") == ["T\n\nЗадач нет."]


def test_task_list_splits_long_lists():
    tasks = [make(i, "Длинная задача " * 15, "Иван") for i in range(1, 80)]
    messages = task_list_messages(tasks, TODAY, "T")
    assert len(messages) > 1 and all(len(m) <= 4000 for m in messages)
